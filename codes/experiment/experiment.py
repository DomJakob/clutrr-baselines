from time import time

import torch
from addict import Dict
import os
import numpy as np

from codes.utils.util import get_device_name
from codes.net.net_registry import choose_model
from codes.net.trainer import Trainer
from codes.utils.data import DataUtility, generate_dictionary
from codes.utils.log import write_metric_logs, write_config_log, write_metadata_logs, write_sequences
from codes.metric.metric_registry import get_metric_dict
from codes.metric.quality_metric import QualityMetric
from codes.onmt.generator import Generator
from codes.utils.experiment_utils import Experiment

import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)



def run_experiment(config, exp):
    write_config_log(config)
    experiment = Experiment()
    parent_dir = os.path.abspath(os.pardir).split('/codes')[0]
    # generate dictionary
    generate_dictionary(config)
    data_util = DataUtility(config)
    base_path = os.path.join(parent_dir, config.dataset.base_path)
    if config.dataset.load_save_path or config.general.mode == 'infer':
        data_util.load(os.path.join(parent_dir, config.dataset.base_path, config.dataset.save_path))
    else:
        data_util.process_data(base_path,
                               config.dataset.train_file)
        data_util.save(os.path.join(parent_dir, config.dataset.base_path, config.dataset.save_path))

    vocab_size = len(data_util.word2id)
    logging.info("Vocab Size : {}".format(vocab_size))
    target_size = len(data_util.target_word2id)
    logging.info("Target size : {}".format(target_size))
    config.model.vocab_size = vocab_size
    config.model.target_size = target_size
    config.model.max_nodes = data_util.max_ents
    logging.info("Max nodes : {}".format(config.model.max_nodes))
    logging.info("Max sentence length : {}".format(data_util.max_sent_length))
    config.model.max_sent_length = data_util.max_sent_length

    logging.info("Loading testing data")
    data_util.process_test_data(base_path, config.dataset.test_files)

    ## set the edge dimension w.r.t the edge encoder
    if config.model.encoder.bidirectional and config.model.graph.edge_embedding == 'lstm':
        config.model.graph.edge_dim = config.model.encoder.hidden_dim * 2
    device = torch.device(get_device_name(device_type=config.general.device))
    experiment.config = config
    experiment.data_util = data_util
    experiment.dataloaders.train = data_util.get_dataloader(mode='train')
    experiment.dataloaders.val = data_util.get_dataloader(mode='val')
    experiment.dataloaders.test = []
    for test_file in config.dataset.test_files:
        experiment.dataloaders.test.append(data_util.get_dataloader(mode='test',
                                                                    test_file=test_file))

    experiment.model.encoder, experiment.model.decoder = choose_model(config)
    experiment.model.encoder = experiment.model.encoder.to(device)
    experiment.model.decoder = experiment.model.decoder.to(device)
    print(experiment.model)
    experiment.trainer = Trainer(
        config.model, experiment.model.encoder,
        experiment.model.decoder,
        max_entity_id=data_util.max_entity_id)

    experiment.optimizers, experiment.schedulers = experiment.trainer.get_optimizers()
    if config.model.should_load_model or config.general.mode == 'infer':
        logging.info("Loading model parameters")
        experiment.load_checkpoint()
    experiment.device = device
    experiment.validation_metrics = get_metric_dict(time_span=10)
    experiment.quality_metrics = QualityMetric(data=data_util)
    experiment.metric_to_perform_early_stopping = config.model.early_stopping.metric_to_track
    experiment.generator = Generator(experiment.data_util, experiment.model,
                                     config, trainer=experiment.trainer)
    experiment.epoch_index = 0
    experiment.iteration_index = Dict()
    experiment.iteration_index.train = 0
    experiment.iteration_index.val = 0
    experiment.iteration_index.test = 0
    experiment.comet_exp = exp

    _run_one_epoch_all_modes(experiment, only_infer=config.general.only_infer)


def _run_epochs(experiment):
    validation_metrics_dict = experiment.validation_metrics
    metric_to_perform_early_stopping = experiment.metric_to_perform_early_stopping
    config = experiment.config
    for key in validation_metrics_dict:
        validation_metrics_dict[key].reset()
    test_acc_per_epoch = []
    while experiment.epoch_index < config.model.num_epochs:
        logging.info("Epoch {}".format(experiment.epoch_index))
        test_accs = _run_one_epoch_all_modes(experiment)
        test_acc_per_epoch.append(test_accs)
        experiment.epoch_index += 1
        for scheduler in experiment.schedulers:
            if config.model.scheduler_type == "exp":
                scheduler.step()
            elif config.model.scheduler_type == "plateau":
                scheduler.step(validation_metrics_dict[metric_to_perform_early_stopping].current_value)
        if config.model.persist_per_epoch > 0 and experiment.epoch_index % config.model.persist_per_epoch == 0:
            experiment.model.save_model(epochs=experiment.epoch_index, optimizers=experiment.optimizers)
    else:
        best_epoch_index = experiment.epoch_index - validation_metrics_dict[metric_to_perform_early_stopping].counter
        write_metadata_logs(best_epoch_index=best_epoch_index)
        print("Best performing model corresponds to epoch id {}".format(best_epoch_index))
        for key, value in validation_metrics_dict.items():
            print("{} of the best performing model = {}".format(
                key, value.get_best_so_far()))
        print("Test score corresponding to best performing epoch id {}".format(best_epoch_index))
        print(', '.join(test_acc_per_epoch[best_epoch_index - 1]))



def _run_one_epoch_all_modes(experiment, only_infer=False):
    if not only_infer:
        if (experiment.dataloaders.train):
            with experiment.comet_ml.train():
                _run_one_epoch(experiment.dataloaders.train, experiment,
                               mode="train",
                               filename=experiment.config.dataset.train_file)
        if (experiment.dataloaders.val):
            with experiment.comet_ml.validate():
                _run_one_epoch(experiment.dataloaders.val, experiment,
                               mode="val",
                               filename=experiment.config.dataset.train_file)
    test_accs = []
    if len(experiment.dataloaders.test) > 0:
        with experiment.comet_ml.test():
            for di, dataloader in enumerate(experiment.dataloaders.test):
                _, acc = _run_one_epoch(dataloader, experiment, mode="test",
                               filename=experiment.config.dataset.test_files[di])
                test_accs.append(str(acc))
    logging.info("------------------------")
    logging.info("> Test accuracies: {}".format(' ,'.join(test_accs)))
    return test_accs


def _run_one_epoch(dataloader, experiment, mode, filename=''):
    trainer = experiment.trainer
    optimizers = experiment.optimizers
    should_train = False
    if (mode == "train"):
        should_train = True

    if should_train:
        trainer.train()
    else:
        trainer.eval()

    aggregated_batch_loss = 0
    num_examples = 0

    true_inp = []
    true_outp = []
    pred_outp = []
    epoch_rel = []

    log_batch_losses = []
    log_batch_rel = []

    for batch_idx, batch in enumerate(dataloader):
        experiment.iteration_index[mode] += 1
        batch.config = experiment.config
        batch.process_adj_mat()
        batch.to_device(experiment.device)

        if (should_train):
            for optimizer in optimizers:
                optimizer.zero_grad()

        outputs, loss = trainer.batchLoss(batch)

        batch_loss = loss.item()
        if (should_train):
            loss.backward()
            for optimizer in optimizers:
                optimizer.step()
        aggregated_batch_loss += (batch_loss * batch.batch_size)

        num_examples += batch.batch_size
        log_batch_losses.append(batch_loss)
        experiment.comet_ml.log_metric("loss", batch_loss, step=batch_idx)

        batch = experiment.generator.process_batch(batch, outputs, beam=False)
        true_inp.extend(batch.true_inp)
        true_outp.extend(batch.true_outp)
        pred_outp.extend(batch.pred_outp)

        accuracy = experiment.quality_metrics.relation_overlap(batch.pred_outp, batch.true_outp)
        experiment.comet_ml.log_metric("accuracy", accuracy, step=batch_idx)
        log_batch_rel.append(accuracy)
        epoch_rel.append(accuracy)

        del batch

    loss = aggregated_batch_loss / num_examples
    epoch_rel = np.mean(epoch_rel)
    if mode == 'val':
        experiment.validation_metrics['val_acc'].update(epoch_rel)
        experiment.validation_metrics['loss'].update(loss)
    logging.info(" -------------------------- ")
    logging.info("Mode : {} , File : {}".format(mode, filename))
    logging.info("Loss : {}, Relation Accuracy : {}".format(
        loss, epoch_rel))

    if mode != 'train':
        # save predicted examples
        true_inp = [' '.join(sent) for sent in true_inp]
        true_outp = [' '.join(sent) for sent in true_outp]
        pred_outp = [' '.join(sent) for sent in pred_outp]
        assert len(true_inp) == len(true_outp) == len(pred_outp)
        write_sequences(true_inp, true_outp, pred_outp, mode, experiment.epoch_index,
                        exp_name=experiment.config.general.exp_name)
    if experiment.config.general.mode == 'train':
        experiment.save_checkpoint(is_best=False)

    return loss, epoch_rel



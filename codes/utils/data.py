# Data preparation and manipulation codes
import torch
import torch.utils.data as data
import re
import pandas as pd
import json
import numpy as np
from nltk.tokenize import word_tokenize, sent_tokenize
from collections import Counter
import pickle as pkl
import itertools as it
from addict import Dict
from codes.net.batch import Batch
from codes.utils.config import get_config
import os
import json
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

base_path = os.path.dirname(os.path.realpath(__file__)).split('codes')[0]
rel_store = json.load(open(os.path.join(base_path, 'codes', 'toy','relations_store.json'),'r'))
#rel_store = json.load(open(os.path.join(base_path, 'codes', 'logic', 'mensus','store.json'),'r'))
RELATION_KEYWORDS = rel_store['_relation_keywords']
UNK_WORD = '<unk>'
PAD_TOKEN = '<pad>'
START_TOKEN = '<s>'
END_TOKEN = '</s>'

class DataUtility():
    """
    Data preparation and utility class
    """
    def __init__(self,
                 config,
                 num_workers=4,
                 common_dict=True):
        """

        :param main_file: file where the summarization resides
        :param train_test_split: default 0.8
        :param sentence_mode: if sentence_mode == True, then split story into sentence
        :param single_abs_line: if True, then output pair is single sentences of abs
        :param num_reads: number of reads for a sentence
        :param dim: dimension of edges
        """
        self.config = config
        # derive configurations
        self.train_test_split = config.dataset.train_test_split
        self.max_vocab = config.dataset.max_vocab
        self.tokenization = config.dataset.tokenization
        self.common_dict = config.dataset.common_dict
        self.batch_size = config.model.batch_size
        self.num_reads = config.model.graph.num_reads
        self.dim = config.model.graph.edge_dim
        self.sentence_mode = config.dataset.sentence_mode
        self.single_abs_line = config.dataset.single_abs_line
        self.only_relation = config.dataset.only_relation

        self.word2id = {}
        self.id2word = {}
        self.target_word2id = {}
        self.target_id2word = {}
        # dict of summary pairs
        self.summaryPairs = {'train':[], 'test':{}}

        self.train_indices = []
        self.test_indices = []
        self.val_indices = []
        self.special_tokens = [PAD_TOKEN, UNK_WORD, START_TOKEN, END_TOKEN]
        self.main_file = ''
        self.common_dict = common_dict
        self.num_workers = num_workers
        # keep some part of the vocab fixed for the entities
        # for that we need to first calculate the max number of unique entities *per row*
        self.train_data = None
        self.test_data = None
        self.train_file = ''
        self.test_file = ''
        self.max_ents = 0
        self.entity_ids = []
        self.max_entity_id = 0
        self.adj_graph = []
        self.dummy_entity = '' # return this entity when UNK entity
        self.load_dictionary = config.dataset.load_dictionary
        self.max_sent_length = 0

    def process_data(self, base_path, train_file, load_dictionary=True):
        """
        Load data and run preprocessing scripts
        :param main_file .csv file of the data
        :return:
        """
        if not train_file.endswith('.csv'):
            self.train_file = os.path.join(base_path, train_file) + '_train.csv'
        else:
            self.train_file = train_file
        train_data = pd.read_csv(self.train_file, comment='#')
        self._check_data(train_data)
        logging.info("Start preprocessing data")
        if load_dictionary:
            dictionary_file = os.path.join(base_path, 'dict.json')
            logging.info("Loading dictionary from {}".format(dictionary_file))
            dictionary = json.load(open(dictionary_file))
            # fix id2word keys
            dictionary['id2word'] = {int(k):v for k,v in dictionary['id2word'].items()}
            dictionary['target_id2word'] = {int(k): v for k, v in dictionary['target_id2word'].items()}
            for key, value in dictionary.items():
                setattr(self, key, value)
        train_data, max_ents_train, = self.process_entities(train_data)
        if not load_dictionary:
            self.max_ents = max_ents_train + 1 # keep an extra dummy entity for unknown entities
        self.preprocess(train_data, mode='train', common_dict=self.common_dict)
        self.train_data = train_data
        self.split_indices()

    def process_test_data(self, base_path, test_files):
        """
        Load testing data
        :param test_files: array of file names
        :return:
        """
        self.test_files = [os.path.join(base_path, t) + '_test.csv' for t in test_files]
        test_datas = [pd.read_csv(tf, comment='#') for tf in self.test_files]
        for test_data in test_datas:
            self._check_data(test_data)
        logging.info("Loaded test data, starting preprocessing")
        p_tests = []
        for ti, test_data in enumerate(test_datas):
            test_data, max_ents_test, = self.process_entities(test_data)
            self.preprocess(test_data, mode='test', common_dict=self.common_dict,
                            test_file=test_files[ti])
            p_tests.append(test_data)
        self.test_data = p_tests
        logging.info("Done preprocessing test data")


    def _check_data(self, data):
        """
        Check if the file has correct headers
        :param data:
        :return:
        """
        assert "story" in list(data.columns)
        assert "summary" in list(data.columns)

    def process_entities(self, data, placeholder='[]'):
        """
        extract entities and replace them with placeholders
        :param placeholder: if [] then simply use regex to extract entities as they are already in
        a placeholder. If None, then use Spacy EntityTokenizer
        :return: max number of entities in dataset
        """
        max_ents = 0
        if placeholder == '[]':
            for i,row in data.iterrows():
                story = row['story']
                summary = row['summary']
                ents = re.findall('\[(.*?)\]', story)
                uniq_ents = set(ents)
                if len(uniq_ents) > max_ents:
                    max_ents = len(uniq_ents)
                for idx, ent in enumerate(uniq_ents):
                    story = story.replace('[{}]'.format(ent), '@ent{}'.format(idx))
                    summary = summary.replace('[{}]'.format(ent), '@ent{}'.format(idx))
                data.at[i, 'story'] = story
                data.at[i, 'summary'] = summary
                data.at[i, 'entities'] =  json.dumps(list(uniq_ents))
        else:
            raise NotImplementedError("Not implemented, should replace with a tokenization policy")
        return data, max_ents

    def preprocess(self, data, mode='train', common_dict=True, single_abs_line=True, test_file=''):
        """
        Usual preprocessing: tokenization, lowercase, and create word dictionaries
        Also, split stories into sentences
        :param single_abs_line: if True, separate the abstracts into its corresponding lines
        and add each story-abstract pairs
        :param common_dict If True, use a common dictionary for inp and output
        :return:
        """

        inp_words = Counter()
        outp_words = Counter()
        max_sent_length = 0
        for i,row in data.iterrows():
            story_sents = sent_tokenize(row['story'])
            summary_sents = sent_tokenize(row['summary'])
            story_sents = [self.tokenize(sent) for sent in story_sents]
            summary_sents = [self.tokenize(sent) for sent in summary_sents]
            max_sl = max([len(s) for s in story_sents])
            if max_sl > max_sent_length:
                max_sent_length = max_sl
            # add in the summaryPairs
            summaryPair = Dict()
            summaryPair.story_sents = story_sents
            summaryPair.summary_sents = summary_sents
            if mode == 'train':
                self.summaryPairs[mode].append(summaryPair)
            else:
                if test_file not in self.summaryPairs[mode]:
                    self.summaryPairs[mode][test_file] = []
                self.summaryPairs[mode][test_file].append(summaryPair)
            story_words = [word for sent in story_sents for word in sent]
            summary_words = [word for sent in summary_sents for word in sent]
            inp_words.update(story_words)
            outp_words.update(summary_words)

        # only assign word-ids in train data
        if mode == 'train' and not self.load_dictionary:
            if common_dict:
                words = inp_words + outp_words
                self.word2id, self.id2word = self.assign_wordids(words)
            else:
                self.word2id['input'], self.id2word['input'] = self.assign_wordids(inp_words)
                self.word2id['output'], self.id2word['output'] = self.assign_wordids(outp_words)

        # get adj graph
        if mode == 'train':
            for sP in self.summaryPairs[mode]:
                sP.story_graph = self.prepare_ent_graph(sP.story_sents)
            logging.info("Processed {} stories in mode {}".format(len(self.summaryPairs[mode]), mode))
            self.max_sent_length = max_sent_length
        else:
            for sP in self.summaryPairs[mode][test_file]:
                sP.story_graph = self.prepare_ent_graph(sP.story_sents)
            logging.info("Processed {} stories in mode {} and file: {}".format(
                len(self.summaryPairs[mode][test_file]), mode, test_file))



    def tokenize(self, sent):
        """
        tokenize sentence based on mode
        :sent - sentence
        :param mode: word/char
        :return: splitted array
        """
        words = []
        if self.tokenization == 'word':
            words = word_tokenize(sent)
        if self.tokenization == 'char':
            words = sent.split('')
        # correct for tokenizing @entity
        corr_w = []
        tmp_w = ''
        for i,w in enumerate(words):
            if w == '@':
                tmp_w = w
            else:
                tmp_w += w
                corr_w.append(tmp_w)
                tmp_w = ''
        return corr_w

    def assign_wordids(self, words, special_tokens=None):
        """
        Given a set of words, create word2id and id2word
        :param words: set of words
        :param special_tokens: set of special tokens to add into dictionary
        :return: word2id, id2word
        """
        count = 0
        word2id = {}
        if not special_tokens:
            special_tokens = self.special_tokens
        ## if max_vocab is not -1, then shrink the word size
        if self.max_vocab >= 0:
            words = [tup[0] for tup in words.most_common(self.max_vocab)]
        else:
            words = list(words.keys())
        # add pad token
        word2id[PAD_TOKEN] = count
        count +=1
        # reserve a block for entities. Record this block for future use.
        start_ent_num = count
        for idx in range(self.max_ents - 1):
            word2id['@ent{}'.format(idx)] = count
            count +=1
        # reserve a dummy entity
        self.dummy_entity = '@ent{}'.format(self.max_ents - 1)
        word2id[self.dummy_entity] = count
        count += 1
        end_ent_num = count
        self.max_entity_id = end_ent_num - 1
        self.entity_ids = list(range(start_ent_num, end_ent_num))
        # add other special tokens
        if special_tokens:
            for tok in special_tokens:
                if tok == PAD_TOKEN:
                    continue
                else:
                    word2id[tok] = count
                    count += 1
        # finally add the words
        for word in words:
            if word not in word2id:
                word2id[word] = count
                count += 1
        # inverse
        id2word = {v: k for k, v in word2id.items()}

        logging.info("Created dictionary. Words : {}, Entities : {}".format(
            len(word2id), len(self.entity_ids)))
        for word in RELATION_KEYWORDS:
            if word not in self.target_word2id:
                last_id = len(self.target_word2id)
                self.target_word2id[word] = last_id
        self.target_id2word = {v:k for k,v in self.target_word2id.items()}
        logging.info("Target Entities : {}".format(len(self.target_word2id)))
        return word2id, id2word

    def split_indices(self):
        """
        Split indices into training and validation
        Now we use separate testing file
        :return:
        """
        indices = range(len(self.summaryPairs['train']))
        mask_i = np.random.choice(indices, int(len(indices) * self.train_test_split), replace=False)
        self.val_indices = [i for i in indices if i not in set(mask_i)]
        self.train_indices = [i for i in indices if i in set(mask_i)]


    def prepare_ent_graph(self, sents, max_nodes=0):
        """
        Given a list of sentences, return an adjacency matrix between entitities
        Assumes entities have the format @ent{num}
        :param sents: list(list(str))
        :param max_nodes: max number of nodes in the adjacency matrix, int
        :return: list(list(int))
        """
        if max_nodes == 0:
            max_nodes = len(self.entity_ids)
        adj_mat = np.zeros((max_nodes, max_nodes))
        for sent in sents:
            ents = list(set([w for w in sent if '@ent' in w]))
            if len(ents) > 1:
                for ent1, ent2 in it.combinations(ents, 2):
                    ent1_id = self.get_entity_id(ent1) - 1
                    ent2_id = self.get_entity_id(ent2) - 1
                    adj_mat[ent1_id][ent2_id] = 1
                    adj_mat[ent2_id][ent1_id] = 1
        return adj_mat

    def get_dataloader(self, mode='train', test_file=''):
        """
        Return a new SequenceDataLoader instance with appropriate rows
        :param mode: train/val/test
        :return: SequenceDataLoader object
        """
        if mode != 'test':
            if mode == 'train':
                indices = self.train_indices
            else:
                indices = self.val_indices
            summaryPairs = self._select(self.summaryPairs['train'], indices)
        else:
            summaryPairs = self.summaryPairs['test'][test_file]


        inp_rows = []
        outp_rows = []
        inp_row_graphs = []
        for summaryPair in summaryPairs:
            inp_row = summaryPair.story_sents
            if not self.sentence_mode:
                inp_row = [word for sent in inp_row for word in sent]
            outp_row = summaryPair.summary_sents
            if self.single_abs_line:
                inp_rows.append(inp_row)
                inp_row_graphs.append(summaryPair.story_graph)
                batch_outp_rows = []
                for outpr in outp_row:
                    batch_outp_rows.append(outpr)
                outp_rows.append(batch_outp_rows)

            else:
                inp_rows.append(inp_row)
                outp_rows.append([word for sent in outp_row for word in sent])
                inp_row_graphs.append(summaryPair.story_graph)

        # check
        assert len(inp_rows) == len(outp_rows) == len(inp_row_graphs)
        logging.info("Total rows : {}, batches : {}".format(len(inp_rows), len(inp_rows) // self.batch_size))

        collate_FN = collate_fn
        if self.sentence_mode:
            collate_FN = sent_collate_fn

        return data.DataLoader(SequenceDataLoader(inp_rows, outp_rows, self,
                                                  inp_graphs=inp_row_graphs),
                               batch_size=self.batch_size,
                               num_workers=self.num_workers,
                               collate_fn=collate_FN)

    def map_text_to_id(self, text):
        if isinstance(text, list):
            return list(map(self.get_token, text))
        else:
            return self.get_token(text)

    def get_token(self, word, target=False):
        if target and word in self.target_word2id:
            return self.target_word2id[word]
        elif word in self.word2id:
            return self.word2id[word]
        else:
            return self.word2id[UNK_WORD]

    def get_entity_id(self, entity):
        if entity in self.word2id:
            return self.word2id[entity]
        else:
            return self.word2id[self.dummy_entity]

    def _filter(self, array, mask):
        """
        filter array based on boolean mask
        :param array: any array
        :param mask: boolean mask
        :return: filtered
        """
        return [array[i] for i,p in enumerate(mask) if p]

    def _select(self, array, indices):
        """
        Select based on indices
        :param array:
        :param indices:
        :return:
        """
        return [array[i] for i in indices]

    def save(self, filename='data_files.pkl'):
        """
        Save the current data utility into pickle file
        :param filename: location
        :return: None
        """
        pkl.dump(self.__dict__, open(filename, 'wb'))
        logging.info("Saved data in {}".format(filename))

    def load(self, filename='data_files.pkl'):
        """
        Load previously saved data utility
        :param filename: location
        :return:
        """
        logging.info("Loading data from {}".format(filename))
        self.__dict__.update(pkl.load(open(filename,'rb')))
        logging.info("Loaded")


class SequenceDataLoader(data.Dataset):
    """
    Separate dataloader instance
    """

    def __init__(self, inp_text, outp_text, data, inp_graphs=None):
        self.inp_text = inp_text
        self.outp_text = outp_text
        self.data = data
        self.inp_graphs = inp_graphs

    def __getitem__(self, index):
        """
        Return single training row for dataloader
        :param item:
        :return:
        """
        inp_row = self.inp_text[index]
        orig_inp = self.inp_text[index]
        inp_row_graph = self.inp_graphs[index]
        inp_row_pos = []
        if self.data.sentence_mode:
            sent_lengths = [len(sent) for sent in inp_row]
            inp_row = [[self.data.get_token(word) for word in sent] for sent in inp_row]
            inp_ents = [[id for id in sent if id in self.data.entity_ids] for sent in inp_row]
            inp_row_pos = [[widx + 1 for widx, word in enumerate(sent)] for sent in inp_row]
        else:
            sent_lengths = [len(inp_row)]
            inp_row = [self.data.get_token(word) for word in inp_row]
            inp_ents = list(set([id for id in inp_row if id in self.data.entity_ids]))

        ## calculate one-hot mask for entities which are used in this row
        flat_inp_ents = inp_ents
        if self.data.sentence_mode:
            flat_inp_ents = [p for x in inp_ents for p in x]
        inp_ent_mask = [1 if idx+1 in flat_inp_ents else 0 for idx in range(len(self.data.entity_ids))]


        # calculate for each entity pair which sentences contain them
        # output should be a max_entity x max_entity x num_sentences --> which should be later padded
        # if not sentence mode, then just output max_entity x max_entity x 1
        num_sents = len(inp_row) # 8, say
        if self.data.sentence_mode:
            assert len(inp_row) == len(inp_ents)
            sentence_pointer = np.zeros((len(self.data.entity_ids), len(self.data.entity_ids),
                                         num_sents))
            for sent_idx, inp_ent in enumerate(inp_ents):
                if len(inp_ent) > 1:
                    for ent1, ent2 in it.combinations(inp_ent, 2):
                        # check if two same entities are not appearing
                        if ent1 == ent2:
                            print("shit")
                            raise NotImplementedError("For now two same entities cannot appear in the same sentence")
                        assert ent1 != ent2
                        # remember we are shifting one bit here
                        sentence_pointer[ent1-1][ent2-1][sent_idx] = 1

        else:
            sentence_pointer = np.ones((len(self.data.entity_ids), len(self.data.entity_ids), 1))


        # calculate the outputs
        outp_row = []
        outp_ents = []
        ent_mask = []
        for row in self.outp_text[index]:
            current_outp_row = [START_TOKEN] + row + [END_TOKEN]
            current_row_ids = [self.data.get_token(word) for word in current_outp_row]
            current_ents = [id for id in current_row_ids if id in self.data.entity_ids]
            if self.data.only_relation:
                # if only relation, then change the output to [START] + [Relation]
                current_outpr = list(set(row).intersection(RELATION_KEYWORDS))
                current_outp_row = [START_TOKEN] + current_outpr #+ [END_TOKEN]
            current_ent_mask = [[1 if w == ent else 0 for w in self.__flatten__(inp_row)] for ent in current_ents]
            current_outp_row = [self.data.get_token(word, target=True) for word in current_outp_row]
            # mask over input sentence
            outp_row.append(current_outp_row)
            if len(current_ents) < 2:
                print(row)
                print(index)
                print(current_ents)
                print(current_row_ids)
                print(self.data.entity_ids)
                raise AssertionError("a sentence must contain two entities")
            outp_ents.append(current_ents)
            ent_mask.append(current_ent_mask)

        return inp_row, outp_row, inp_ents, outp_ents, ent_mask, inp_row_graph, \
               sent_lengths, inp_ent_mask, sentence_pointer, orig_inp, inp_row_pos

    def __flatten__(self, arr):
        if any(isinstance(el, list) for el in arr):
            return [a for b in arr for a in b]
        else:
            return arr

    def __len__(self):
        return len(self.inp_text)


## Helper functions
def simple_merge(rows):
    lengths = [len(row) for row in rows]
    padded_rows = pad_rows(rows, lengths)
    return padded_rows, lengths

def nested_merge(rows):
    lengths = []
    for row in rows:
        row_length = [len(current_row) for current_row in row]
        lengths.append(row_length)

    # lengths = [len(row) for row in rows]
    padded_rows = pad_nested_row(rows, lengths)
    return padded_rows, lengths

def simple_np_merge(rows):
    lengths = [len(row) for row in rows]
    padded_rows = pad_rows(rows, lengths)
    return padded_rows, lengths

def collate_fn(data):
    """
    helper function for torch.DataLoader
    :param data: list of tuples (inp, outp)
    :return:
    """
    ## sort dataset by inp sentences
    data.sort(key=lambda x: len(x[0]), reverse=True)
    inp_data, outp_data, inp_ents, outp_ents, ent_mask, inp_graphs, sent_lengths, inp_ent_mask, *_ = zip(*data)
    inp_data, inp_lengths = simple_merge(inp_data)
    # outp_data, outp_lengths = simple_merge(outp_data)
    outp_data, outp_lengths = nested_merge(outp_data)

    # outp_data = outp_data.view(-1, outp_data.shape[2]) no need to reshape now, will do it later
    outp_ents = torch.LongTensor(outp_ents)
    # outp_ents = outp_ents.view(-1, outp_ents.shape[2])
    ent_mask = pad_nested_ents(ent_mask, inp_lengths)

    # prepare batch
    batch = Batch(
        inp=inp_data,
        inp_lengths=inp_lengths,
        sent_lengths=sent_lengths,
        outp=outp_data,
        outp_lengths=outp_lengths,
        inp_ents=inp_ents,
        outp_ents=outp_ents,
        inp_graphs=torch.LongTensor(inp_graphs),
        ent_mask= ent_mask,
        inp_ent_mask = torch.LongTensor(inp_ent_mask)
    )

    return batch

def sent_merge(rows, sent_lengths):
    lengths = [len(row) for row in rows]
    max_sent_l = max([n for sentl in sent_lengths for n in sentl])
    padded_rows = torch.zeros(len(rows), max(lengths), max_sent_l).long()
    for i,row in enumerate(rows):
        end = lengths[i]
        for j,sent_row in enumerate(row):
            padded_rows[i, j, :sent_lengths[i][j]] = torch.LongTensor(sent_row)
    return padded_rows, lengths

def sent_collate_fn(data):
    """
    helper function for torch.DataLoader
    modified to handle sentences
    :param data: list of tuples (inp, outp)
    :return:
    """

    ## sort dataset by number of sentences
    data.sort(key=lambda x: len(x[0]), reverse=True)
    inp_data, outp_data, inp_ents, outp_ents, ent_mask, inp_graphs\
        , sent_lengths, inp_ent_mask, sentence_pointer\
        , orig_inp, inp_row_pos = zip(*data)

    inp_data, inp_lengths = sent_merge(inp_data, sent_lengths)
    inp_row_pos, _ = sent_merge(inp_row_pos, sent_lengths)
    max_node, _, _ = sentence_pointer[0].shape
    sentence_pointer = [sp.reshape(-1, sp.shape[2]) for sp in sentence_pointer]
    sentence_pointer = [sp.tolist() for sp in sentence_pointer]
    sentence_pointer = [s for sp in sentence_pointer for s in sp] # flatten
    sentence_pointer, sent_lens = simple_merge(sentence_pointer)
    sentence_pointer = sentence_pointer.view(inp_data.size(0), max_node, max_node, -1)

    sent_lengths = pad_sent_lengths(sent_lengths)

    outp_data, outp_lengths = nested_merge(outp_data)
    outp_ents = torch.LongTensor(outp_ents)
    ent_mask = pad_nested_ents(ent_mask, inp_lengths)

    # prepare batch
    batch = Batch(
        inp=inp_data,
        inp_lengths=inp_lengths,
        sent_lengths=sent_lengths,
        outp=outp_data,
        outp_lengths=outp_lengths,
        inp_ents=inp_ents,
        outp_ents=outp_ents,
        inp_graphs=torch.LongTensor(inp_graphs),
        ent_mask=ent_mask,
        sentence_pointer=sentence_pointer,
        orig_inp = orig_inp,
        inp_ent_mask = torch.LongTensor(inp_ent_mask),
        inp_row_pos = inp_row_pos
    )


    return batch

def pad_rows(rows, lengths):
    padded_rows = torch.zeros(len(rows), max(lengths)).long()
    for i, row in enumerate(rows):
        end = lengths[i]
        padded_rows[i, :end] = torch.LongTensor(row[:end])
    return padded_rows

def pad_nested_row(rows, lengths):
    max_abstract_length = max([l for ln in lengths for l in ln])
    max_num_abstracts = max(list(map(len, rows)))
    padded_rows = torch.zeros(len(rows), max_num_abstracts, max_abstract_length).long()
    for i, row in enumerate(rows):
        for j, abstract in enumerate(row):
            end = lengths[i][j]
            padded_rows[i, j, :end] = torch.LongTensor(row[j][:end])
    return padded_rows


def pad_ents(ents, lengths):
    padded_ents = torch.zeros((len(ents), max(lengths), 2)).long()
    for i, row in enumerate(ents):
        end = lengths[i]
        for ent_n in range(len(row)):
            padded_ents[i, :end, ent_n] = torch.LongTensor(row[ent_n][:end])
    return padded_ents

def pad_nested_ents(ents, lengths):
    abstract_lengths = []
    batch_size = len(ents)
    abstracts_per_batch = len(ents[0])
    num_entities = len(ents[0][0])
    abstract_lengths = []
    for row in ents:
        row_length = [len(abstract_line[0]) for abstract_line in row]
        abstract_lengths.append(row_length)
    abstract_lengths = [a for c in abstract_lengths for a in c]
    max_abstract_length = max(abstract_lengths)
    padded_ents = torch.zeros(batch_size, abstracts_per_batch, num_entities, max_abstract_length).long()
    for i, batch_row in enumerate(ents):
        for j, abstract in enumerate(batch_row):
            for ent_n in range(len(abstract)):
                end = lengths[i]
                padded_ents[i, j, ent_n, :end] = torch.LongTensor(batch_row[j][ent_n][:end])
    return padded_ents

def pad_sent_lengths(sent_lens):
    """
    given sentence lengths, pad them so that the total batch length is equal
    :return:
    """
    max_len = max([len(sent) for sent in sent_lens])
    pad_lens = []
    for sent in sent_lens:
        pad_lens.append(sent + [0]*(max_len - len(sent)))
    return pad_lens

def generate_dictionary(config):
    """
    Before running an experiment, make sure that a dictionary
    is generated
    Check if the dictionary is present, if so then return
    :return:
    """
    parent_dir = os.path.abspath(os.pardir).split('/codes')[0]
    base_path = os.path.join(parent_dir, config.dataset.base_path)
    dictionary_file = os.path.join(parent_dir, config.dataset.base_path, 'dict.json')
    if os.path.isfile(dictionary_file):
        logging.info("Dictionary present at {}".format(dictionary_file))
        return
    logging.info("Creating dictionary with all test files")
    ds = DataUtility(config)
    for test_file in config.dataset.test_files:
        trainfl = os.path.join(base_path, test_file) + '_train.csv'
        testfl = os.path.join(base_path, test_file) + '_test.csv'
        ds.process_data(os.path.join(parent_dir, config.dataset.base_path),
                        trainfl,load_dictionary=False)
        ds.process_data(os.path.join(parent_dir, config.dataset.base_path),
                        testfl, load_dictionary=False)

    # save dictionary
    dictionary_file = os.path.join(parent_dir, config.dataset.base_path, 'dict.json')
    dictionary = {
        'word2id': ds.word2id,
        'id2word': ds.id2word,
        'target_word2id': ds.target_word2id,
        'target_id2word': ds.target_id2word,
        'max_ents': ds.max_ents,
        'max_vocab': ds.max_vocab,
        'max_entity_id': ds.max_entity_id,
        'entity_ids': ds.entity_ids,
        'dummy_entitiy': ds.dummy_entity
    }
    json.dump(dictionary, open(dictionary_file,'w'))
    logging.info("Saved dictionary at {}".format(dictionary_file))

if __name__ == '__main__':
    # Generate a dictionary once and re-use it over again
    # We do this to resolve the issue of unknown elements in generalizability
    # experiments
    # Take the last training file which has the longest path and make a dictionary
    parent_dir = os.path.abspath(os.pardir).split('/codes')[0]
    config = get_config(config_id='graph_rel')
    generate_dictionary(config)




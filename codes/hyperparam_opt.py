import itertools as it
from collections import namedtuple
from codes.utils.config import get_config
import os
from copy import deepcopy
import yaml
import json
import operator
from functools import reduce
from codes.utils.util import flatten_dictionary


def getFromDict(dataDict, mapList):
    return reduce(operator.getitem, mapList, dataDict)

def setInDict(dataDict, mapList, value):
    getFromDict(dataDict, mapList[:-1])[mapList[-1]] = value

def create_list_of_Hyperparams(hyperparams_dict):
    sep = "$$"
    Hyperparam = namedtuple("Hyperparam", ["key_list", "value"])
    flattend_dict = flatten_dictionary(hyperparams_dict)
    Hyperparam_list = []
    for key, val_list in flattend_dict.items():
        temp_list = []
        keylist = key.split(sep)
        for val in val_list:
            temp_list.append(Hyperparam(keylist, val))
        Hyperparam_list.append(temp_list)
    return it.product(*Hyperparam_list, repeat=1)

def create_configs(config_id):
    base_config = get_config(config_id=config_id)
    current_id = 0
    # for general
    hyperparams_dict = {
        "model": {
            "optimiser":{
                "learning_rate": [0.1, 0.01, 0.001, 0.0001]
            },
            "embedding": {
                    "dim": [50, 100, 150, 200, 250, 300]
            }
        }
    }

    if config_id == 'rn':
        # for bilstm
        hyperparams_dict.update({
            "model": {
                "rn": {
                    "g_theta_dim": [64, 128, 256],
                    "f_theta": {
                        "dim_1": [64, 128, 256, 512],
                        "dim_2": [64, 128, 256, 512]
                    }
                }
            }
        })

    if config_id == 'rn_tpr':
        hyperparams_dict.update({
            "model": {
                "rn": {
                    "g_theta_dim": [64, 128, 256],
                    "f_theta": {
                        "dim_1": [64, 128, 256, 512],
                        "dim_2": [64, 128, 256, 512]
                    }
                }
            }
        })

    if config_id == 'mac':
        hyperparams_dict.update({
            "model": {
                "rn": {
                    "g_theta_dim": [64, 128, 256],
                    "f_theta": {
                        "dim_1": [64, 128, 256, 512],
                        "dim_2": [64, 128, 256, 512]
                    }
                }
            }
        })

    if config_id == 'gat_clean':
        hyperparams_dict.update({
            "model": {
                "graph": {
                    "message_dim": [50, 100, 150, 200],
                    "num_message_rounds": [1,2,3,4,5]
                }
            }
        })


    path = os.path.dirname(os.path.realpath(__file__)).split('/codes')[0]
    target_dir = os.path.join(path, "config")

    for hyperparams in create_list_of_Hyperparams(hyperparams_dict):
        new_config = deepcopy(base_config)
        current_str_id = config_id + "_hp_" + str(current_id)
        new_config["general"]["id"] = current_str_id
        for hyperparam in hyperparams:
            setInDict(new_config, hyperparam.key_list, hyperparam.value)
        new_config_file = target_dir + "/{}.yaml".format(current_str_id)
        with open(new_config_file, "w") as f:
            f.write(yaml.dump(yaml.load(json.dumps(new_config)), default_flow_style=False))
        current_id += 1

if __name__ == '__main__':
    create_configs('bilstm')
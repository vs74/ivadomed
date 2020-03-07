##############################################################
#
# This script automates the training  of a networks on multiple GPUs to deal with hyperparameter optimisation
#
# Usage: python dev/training_scheduler.py -c path/to/config.json -g number_of_gpus
#
# Contributors: olivier
#
##############################################################

import argparse
import copy
import joblib
import json
import logging
import numpy as np
import pandas as pd
import sys
import torch.multiprocessing as mp
#import time

from ivadomed import main as ivado
from ivadomed import loader
from itertools import product
from scipy.stats import ttest_ind_from_stats

LOG_FILENAME = 'log.txt'
logging.basicConfig(filename=LOG_FILENAME, level=logging.DEBUG)


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", required=True, help="Base config file path.")
    parser.add_argument("-n", "--n-iterations", dest="n_iterations",
                        type=int, help="Number of times to run each config .")
    parser.add_argument("--all-combin", dest='all_combin', action='store_true',
                        help="To run all combinations of config")
    parser.set_defaults(all_combin=False)

    return parser


def worker(config):
    current = mp.current_process()
    # ID of process used to assign a GPU
    ID = int(current.name[-1]) - 1

    # Use GPU i from the array specified in the config file
    config["gpu"] = config["gpu"][ID]

    # Call ivado cmd_train
    try:
        # Save best validation score
        best_training_dice, best_training_loss, best_validation_dice, best_validation_loss = ivado.cmd_train(
            config)
    except:
        logging.exception('Got exception on main handler')
        print("Unexpected error:", sys.exc_info()[0])
        raise

    # Save config file in log directory
    config_copy = open(config["log_directory"] + "/config.json", "w")
    json.dump(config, config_copy, indent=4)

    return config["log_directory"], best_training_dice, best_training_loss, best_validation_dice, best_validation_loss


def test_worker(config):
    return config["log_directory"], np.random.normal(0, 1), np.random.normal(0, 1), np.random.normal(0, 1), np.random.normal(0, 1)


if __name__ == '__main__':

    parser = get_parser()
    args = parser.parse_args()

    # Load initial config
    with open(args.config, "r") as fhandle:
        initial_config = json.load(fhandle)

    # Hyperparameters values to test

    # Step 1 : batch size, initial LR and LR scheduler

    batch_sizes = [8, 16, 32, 64]
    initial_lrs = [1e-2, 1e-3, 1e-4, 1e-5]

    """
    lr_schedulers = [{"name": "CosineAnnealingLR"},
                    {"name": "CosineAnnealingWarmRestarts", "T_0": 10}
                    {"name": "CyclicLR", "base_lr" : X, "max_lr" : Y}]
    """

    # Step 2 : Losses (dice, cross_entropy, focal, mixed, gdl)

    #losses = [{"name": "dice"}, {"name": "cross_entropy"}, {"name": "gdl"}]

    # Focal loss
    """
    base_loss = {"name": "focal", "params": {"gamma": 0.5, "alpha" : 0.2}}
    alphas = [0.2, 0.5, 0.75, 1]
    gammas = [0.5, 1, 1.5, 2]
    for combination in product(*[alphas, gammas]):
        new_loss = copy.deepcopy(base_loss)
        new_loss["params"]["alpha"] = combination[0]
        new_loss["params"]["gamma"] = combination[1]
        losses.append(new_loss)
    #print(losses)
    """

    # Focal dice

    """
    base_loss = {"name": "focal_dice", "params": {"gamma": 0.5, "alpha" : 0.2, beta : "1"}}
    betas = [0.25, 0.5, 1, 2, 4]
    for beta in betas:
        new_loss = copy.deepcopy(base_loss)
        new_loss["params"]["beta"] = beta
        losses.append(new_loss)
    #print(losses)
    """

    # Step 3 : FiLM

    #metadatas = ["contrast"]

    # film_layers = [ [1, 0, 0, 0, 0, 0, 0, 0],
    #                [0, 0, 0, 0, 1, 0, 0, 0],
    #                [0, 0, 0, 0, 0, 0, 0, 1],
    #                [1, 1, 1, 1, 1, 1, 1, 1]]

    # Step 4 : Mixup

    #mixup_bools = [True]
    #mixup_alphas = [0.5, 1, 2]

    # Step 5 : Dilation

    #gt_dilations = [0, 0.5, 1]

    # Split dataset if not already done
    if initial_config.get("split_path") is None:
        train_lst, valid_lst, test_lst = loader.split_dataset(path_folder=initial_config["bids_path"],
                                                              center_test_lst=initial_config["center_test"],
                                                              split_method=initial_config["split_method"],
                                                              random_seed=initial_config["random_seed"],
                                                              train_frac=initial_config["train_fraction"],
                                                              test_frac=initial_config["test_fraction"])

        # save the subject distribution
        split_dct = {'train': train_lst, 'valid': valid_lst, 'test': test_lst}
        split_path = "./"+"common_split_datasets.joblib"
        joblib.dump(split_dct, split_path)
        initial_config["split_path"] = split_path

    # Dict with key corresponding to name of the param in the config file
    param_dict = {"batch_size": batch_sizes, "initial_lr": initial_lrs}

    config_list = []
    # Test all combinations (change multiple parameters for each test)
    if args.all_combin:

        # Cartesian product (all combinations)
        combinations = (dict(zip(param_dict.keys(), values))
                        for values in product(*param_dict.values()))

        for combination in combinations:

            new_config = copy.deepcopy(initial_config)

            for param in combination:

                value = combination[param]
                new_config[param] = value
                new_config["log_directory"] = new_config["log_directory"] + \
                    "-" + param + "=" + str(value)

            config_list.append(copy.deepcopy(new_config))
    # Change a single parameter for each test
    else:
        for param in param_dict:

            new_config = copy.deepcopy(initial_config)

            for value in param_dict[param]:

                new_config[param] = value
                new_config["log_directory"] = initial_config["log_directory"] + \
                    "-" + param + "=" + str(value)
                config_list.append(copy.deepcopy(new_config))

    # CUDA problem when forking process
    # https://github.com/pytorch/pytorch/issues/2517
    mp.set_start_method('spawn')

    # Run all configs on a separate process, with a maximum of n_gpus  processes at a given time
    pool = mp.Pool(processes=len(initial_config["gpu"]))
    if(args.n_iterations is not None):
        n_iterations = args.n_iterations
    else:
        n_iterations = 1
    # print(n_iterations)
    #all_scores = []
    results_df = pd.DataFrame()
    for i in range(n_iterations):
        validation_scores = pool.map(worker, config_list)
        # all_scores.append(validation_scores)
        temp_df = pd.DataFrame(validation_scores, columns=[
                               'log_directory', 'best_training_dice', 'best_training_loss', 'best_validation_dice', 'best_validation_loss'])
        results_df = pd.concat([results_df, temp_df])


# Do avg, std, p-values
# Assuming base case is first case
    if(n_iterations > 1):
        avg = results_df.groupby(['log_directory']).mean()
        std = results_df.groupby(['log_directory']).std()
        print(results_df)
        print(avg, std)

        p_values = np.zeros((len(config_list), len(config_list)))
        i,j = 0,0
        for confA in config_list:
            print(confA["log_directory"])
            j = 0
            for confB in config_list:
                p_values[i, j] = ttest_ind_from_stats(mean1=avg.loc[confA["log_directory"]]["best_validation_dice"], std1=std.loc[confA["log_directory"]]["best_validation_dice"],
                                                      nobs1=n_iterations, mean2=avg.loc[confB["log_directory"]]["best_validation_dice"], std2=std.loc[confB["log_directory"]]["best_validation_dice"], nobs2=n_iterations).pvalue
                j += 1
            i += 1

        print(p_values)

    # Merge config and results in a df
    config_df = pd.DataFrame.from_dict(config_list)
    keep = list(param_dict.keys())
    keep.append("log_directory")
    config_df = config_df[keep]

    results_df = config_df.set_index('log_directory').join(results_df.set_index('log_directory'))
    results_df = results_df.sort_values(by=['best_validation_loss'])

    results_df.to_pickle("output_df.pkl")
    print(results_df)

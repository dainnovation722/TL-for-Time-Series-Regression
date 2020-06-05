import random
import argparse
import json
import math
from os import path, getcwd, makedirs, environ, listdir

import tensorflow as tf
import numpy as np
import keras
from sklearn.metrics import mean_squared_error as mse
from sklearn.model_selection import train_test_split
from keras.models import load_model
from keras.callbacks import CSVLogger, ModelCheckpoint, ReduceLROnPlateau
from recombinator.optimal_block_length import optimal_block_length
from recombinator.block_bootstrap import circular_block_bootstrap

from utils.model import build_model
from utils.data_io import (
    read_data_from_dataset,
    generator,
    split_dataset,
    ReccurentTrainingGenerator,
    ReccurentPredictingGenerator,
    decompose_time_series
)
from utils.save import save_lr_curve, save_prediction_plot, save_yy_plot, save_mse
from utils.output import metrics


def seed_every_thing(seed=1234):
    environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    random.seed(seed)
    tf.random.set_random_seed(seed)


def parse_arguments():
    ap = argparse.ArgumentParser(
        description='Time-Series Regression by LSTM through transfer learning')
    # for dataset path
    ap.add_argument('--out-dir', '-o', default='result',
                    type=str, help='path for output directory')
    # for model
    ap.add_argument('--seed', type=int, default=1234,
                    help='seed value for random value, (default : 1234)')
    ap.add_argument('--train-ratio', default=0.8, type=float,
                    help='percentage of train data to be loaded (default : 0.9)')
    ap.add_argument('--time-window', default=1000, type=int,
                    help='length of time to capture at once (default : 1000)')
    # for training
    ap.add_argument('--train-mode', '-m', default='pre-train', type=str,
                    help='"pre-train", "transfer-learning", "without-transfer-learning", \
                            "bagging", "noise-injection", "score" (default : pre-train)')
    ap.add_argument('--gpu', action='store_true',
                    help='whether to do calculations on gpu machines (default : False)')
    ap.add_argument('--nb-epochs', default=1, type=int, 
                    help='training epochs for the model (default : 1)')
    ap.add_argument('--nb-batch', default=20, type=int,
                    help='number of batches in training (default : 20)')
    ap.add_argument('--nb-subset', default=10, type=int,
                    help='number of data subset in bootstrapping (default : 10)')
    ap.add_argument('--noise-var', default=0.0001, type=float,
                    help='variance of noise in noise injection (default : 0.0001)')
    ap.add_argument('--valid-ratio', default=0.2, type=float,
                    help='ratio of validation data in train data (default : 0.2)')
    ap.add_argument('--freeze', action='store_true', 
                    help='whether to freeze transferred weights in transfer learning (default : False)')
    # for output
    ap.add_argument('--train-verbose', default=1, type=int,
                    help='whether to show the learning process (default : 1)')
    args = vars(ap.parse_args())
    return args


def save(model, y_test_time, y_pred_test_time, write_result_out_dir):
    save_lr_curve(model, write_result_out_dir)
    save_prediction_plot(y_test_time, y_pred_test_time, write_result_out_dir)
    save_yy_plot(y_test_time, y_pred_test_time, write_result_out_dir)
    save_mse(y_test_time, y_pred_test_time, write_result_out_dir, model=model)
    

def calcu_time_window(train: np.array, test: np.array, time_window: int):
    if train.shape[0] > test.shape[0]:
        time_steps = time_window if test.shape[0] // 2 > time_window else test.shape[0] // 2  # Need to optimize time_window...
    else:
        time_steps = time_window if train.shape[0] // 2 > time_window else train.shape[0] // 2
    return time_steps


def adjust_to_range(data: np.array):
    data = np.where(data > 1, 1, data)
    data = np.where(data < 0, 0, data)
    return data


def load_dataset(data_dir_path, time_window, pre_train=None, bootstrap=None, noise=None, nb_subset=10, var=0.0001):
    '''load dataset compatible with LSTM model from specified path'''
    X_train, y_train, X_test, y_test = read_data_from_dataset(data_dir_path)
    time_steps = calcu_time_window(X_train, X_test, time_window)
    
    # in case of pre-training (no use for test, so concatenate train and test)
    if pre_train:
        X_train = np.concatenate((X_train, X_test), axis=0)
        y_train = np.concatenate((y_train, y_test), axis=0)
        X_train_time, y_train_time = generator(X_train, y_train, time_steps)
        return X_train_time, y_train_time
    
    # in case of bootstrap (make some data subset from original data)
    if bootstrap:
        b_star = optimal_block_length(y_train)
        b_star_cb = math.ceil(b_star[0].b_star_cb)
        print(f'optimal block length for circular bootstrap = {b_star_cb}')
        np.random.seed(0)
        y_train_subsets = circular_block_bootstrap(y_train, block_length=b_star_cb, 
                                                   replications=nb_subset, replace=True)
        X_train_subsets = []
        for i in range(X_train.shape[1]):
            np.random.seed(0)
            X_cb = circular_block_bootstrap(X_train[:,i], block_length=b_star_cb,
                                            replications=nb_subset, replace=True)
            
            X_train_subsets.append(X_cb)
        X_train_subsets = np.array(X_train_subsets)
        X_train_subsets = X_train_subsets.transpose(1, 2, 0)

        X_train_time, y_train_time = [], []
        for i_subset in range(nb_subset):
            i_X_train_time, i_y_train_time = \
                generator(X_train_subsets[i_subset], y_train_subsets[i_subset], time_steps)
            X_train_time.append(i_X_train_time)
            y_train_time.append(i_y_train_time)
        X_test_time, y_test_time = generator(X_test, y_test, time_steps)
        return np.array(X_train_time), np.array(y_train_time), X_test_time, y_test_time

    if noise:
        np.random.normal(0)
        X_train = X_train + np.random.normal(scale=var, size=X_train.shape)
        X_train = adjust_to_range(X_train)
        
    X_train_time, y_train_time = generator(X_train, y_train, time_steps)
    X_test_time, y_test_time = generator(X_test, y_test, time_steps)
    return X_train_time, y_train_time, X_test_time, y_test_time


def make_callbacks(file_path):

    reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.1, patience=5, min_lr=0.000001)
    model_checkpoint = ModelCheckpoint(filepath=file_path, monitor='val_loss', save_best_only=True)
    csv_logger = CSVLogger(path.join(path.dirname(file_path), 'epoch_log.csv'))
    callbacks = [reduce_lr, model_checkpoint, csv_logger]
    return callbacks
 

def main():

    # get args
    args = parse_arguments()

    # set seed
    seed_every_thing(args["seed"])
    
    # make output base directory
    write_out_dir = path.normpath(path.join(getcwd(), 'reports', args["out_dir"]))
    makedirs(write_out_dir, exist_ok=True)
    
    # save arguments
    path_arguments = path.join(write_out_dir, 'params.json')
    if not path.exists(path_arguments):
        with open(path_arguments, mode="w") as f:
            json.dump(args, f, indent=4)

    print('-' * 140)
    
    if args["train_mode"] == 'pre-train':
        
        for source in listdir('dataset/source'):

            # skip source dataset without pickle file
            data_dir_path = path.join('dataset', 'source', source)
            if not path.exists(f'{data_dir_path}/X_train.pkl'): continue
            
            # make output directory
            write_result_out_dir = path.join(write_out_dir, args["train_mode"], source)
            makedirs(write_result_out_dir, exist_ok=True)
            
            # load dataset
            X_train, y_train, X_test, y_test = \
                read_data_from_dataset(data_dir_path)
            X_train = np.concatenate((X_train, X_test), axis=0)  # no need for test data when pre-training
            y_train = np.concatenate((y_train, y_test), axis=0)  # no need for test data when pre-training
            X_train, X_valid, y_train, y_valid =  \
                train_test_split(X_train, y_train, test_size=args["valid_ratio"], shuffle=False)
            print(f'\nSource dataset : {source}')
            print(f'\nX_train : {X_train.shape[0]}')
            print(f'\nX_valid : {X_valid.shape[0]}')
            
            # construct the model
            file_path = path.join(write_result_out_dir, 'best_model.hdf5')
            callbacks = make_callbacks(file_path)
            _, period = decompose_time_series(y_train)
            input_shape = (period, X_train.shape[1])
            model = build_model(input_shape, args["gpu"], write_result_out_dir)
            
            # train the model
            bsize = len(y_train) // args["nb_batch"]
            RTG = ReccurentTrainingGenerator(X_train, y_train, batch_size=bsize, timesteps=period, delay=1)
            RVG = ReccurentTrainingGenerator(X_valid, y_valid, batch_size=bsize, timesteps=period, delay=1)
            H = model.fit_generator(RTG, validation_data=RVG, epochs=args["nb_epochs"], verbose=1, callbacks=callbacks)
            save_lr_curve(H, write_result_out_dir)
            

            # clear memory up
            keras.backend.clear_session()
            print('\n' * 2 + '-' * 140 + '\n' * 2)
    
    elif args["train_mode"] == 'transfer-learning':
        
        for target in listdir('dataset/target'):
        
            # skip target in the absence of pickle file
            if not path.exists(f'dataset/target/{target}/X_train.pkl'): continue

            for source in listdir(f'{write_out_dir}/pre-train'):
                
                # skip target dataset if target is source
                if target == source: continue
                
                # make output directory
                write_result_out_dir = path.join(write_out_dir, args["train_mode"], target, source)
                makedirs(write_result_out_dir, exist_ok=True)
                    
                # load dataset
                data_dir_path = f'dataset/target/{target}'
                X_train, y_train, X_test, y_test = \
                    read_data_from_dataset(data_dir_path)
                X_train, X_valid, y_train, y_valid = \
                    train_test_split(X_train, y_train, test_size=args["valid_ratio"], shuffle=False)
                print(f'\nTarget dataset : {target}')
                print(f'\nX_train : {X_train.shape[0]}')
                print(f'\nX_valid : {X_valid.shape[0]}')
                print(f'\nX_test : {X_test.shape[0]}')
                
                # load pre-trained model
                pre_model = load_model(f'{write_out_dir}/pre-train/{source}/best_model.hdf5')
                
                # construct the model
                file_path = path.join(write_result_out_dir, 'transferred_best_model.hdf5')
                callbacks = make_callbacks(file_path)
                _, period = decompose_time_series(y_train)
                input_shape = (period, X_train.shape[1])
                model = build_model(input_shape, args["gpu"], write_result_out_dir, pre_model=pre_model, freeze=args["freeze"])
        
                # train the model
                bsize = len(y_train) // args["nb_batch"]
                RTG = ReccurentTrainingGenerator(X_train, y_train, batch_size=bsize, timesteps=period, delay=1)
                RVG = ReccurentTrainingGenerator(X_valid, y_valid, batch_size=bsize, timesteps=period, delay=1)
                H = model.fit_generator(RTG, validation_data=RVG, epochs=args["nb_epochs"], verbose=1, callbacks=callbacks)
                save_lr_curve(H, write_result_out_dir)
                
                # prediction
                best_model = load_model(file_path)
                RPG = ReccurentPredictingGenerator(X_test, batch_size=bsize, timesteps=period)
                y_test_pred = best_model.predict_generator(RPG)

                # save log for the model
                y_test = y_test[-len(y_test_pred):]
                save_prediction_plot(y_test, y_test_pred, write_result_out_dir)
                save_yy_plot(y_test, y_test_pred, write_result_out_dir)
                save_mse(y_test, y_test_pred, write_result_out_dir, model=model)
                keras.backend.clear_session()
                print('\n' * 2 + '-' * 140 + '\n' * 2)
    
    elif args["train_mode"] == 'without-transfer-learning':

        for target in listdir('dataset/target'):
        
            # make output directory
            write_result_out_dir = path.join(write_out_dir, args["train_mode"], target)
            makedirs(write_result_out_dir, exist_ok=True)

            # skip source dataset without pickle file
            data_dir_path = path.join('dataset', 'target', target)
            if not path.exists(f'{data_dir_path}/X_train.pkl'): continue
            
            # load dataset
            X_train, y_train, X_test, y_test = \
                read_data_from_dataset(data_dir_path)
            X_train = np.concatenate((X_train, X_test), axis=0)  # no need for test data when pre-training
            y_train = np.concatenate((y_train, y_test), axis=0)  # no need for test data when pre-training
            X_train, X_valid, y_train, y_valid =  \
                train_test_split(X_train, y_train, test_size=args["valid_ratio"], shuffle=False)
            print(f'\nTarget dataset : {target}')
            print(f'\nX_train : {X_train.shape[0]}')
            print(f'\nX_valid : {X_valid.shape[0]}')
            
            # construct the model
            file_path = path.join(write_result_out_dir, 'best_model.hdf5')
            callbacks = make_callbacks(file_path)
            _, period = decompose_time_series(y_train)
            input_shape = (period, X_train.shape[1])
            model = build_model(input_shape, args["gpu"], write_result_out_dir)
            
            # train the model
            bsize = len(y_train) // args["nb_batch"]
            RTG = ReccurentTrainingGenerator(X_train, y_train, batch_size=bsize, timesteps=period, delay=1)
            RVG = ReccurentTrainingGenerator(X_valid, y_valid, batch_size=bsize, timesteps=period, delay=1)
            H = model.fit_generator(RTG, validation_data=RVG, epochs=args["nb_epochs"], verbose=1, callbacks=callbacks)
            save_lr_curve(H, write_result_out_dir)

            # save log for the model
            y_test = y_test[-len(y_test_pred):]
            save_prediction_plot(y_test, y_test_pred, write_result_out_dir)
            save_yy_plot(y_test, y_test_pred, write_result_out_dir)
            save_mse(y_test, y_test_pred, write_result_out_dir, model=model)

            # clear memory up
            keras.backend.clear_session()
            print('\n' * 2 + '-' * 140 + '\n' * 2)

    elif args["train_mode"] == 'bagging':
    
        for target in listdir('dataset/target'):
            if target == 'debutanizer': continue
            # make output directory
            write_result_out_dir = path.join(write_out_dir, target)
            makedirs(write_result_out_dir, exist_ok=True)

            # skip source dataset without pickle file
            data_dir_path = path.join('dataset', 'target', target)
            if not path.exists(f'{data_dir_path}/X_train.pkl'): continue
            
            # load dataset
            X_train_time, y_train_time, X_test_time, y_test_time = \
                load_dataset(data_dir_path, time_window, bootstrap=True, nb_subset=args["nb_subset"])

            y_pred_test_time = []
            for i_subset, (i_X_train_time, i_y_train_time) in enumerate(zip(X_train_time, y_train_time)):
                i_X_train_time, i_y_train_time, i_X_valid_time, i_y_valid_time = \
                    split_dataset(i_X_train_time, i_y_train_time, ratio=args["train_ratio"])
                # construct the model
                model_dir = path.join(write_result_out_dir, 'model')
                makedirs(model_dir, exist_ok=True)
                file_path = path.join(model_dir, f'best_model_{i_subset}.hdf5')
                callbacks = make_callbacks(file_path)
                input_shape = (i_X_train_time.shape[1], i_X_train_time.shape[2])  # x_train.shape[2] is number of variable
                model = regressor(input_shape, args["gpu"], write_result_out_dir)
                # train the model
                model.fit(i_X_train_time, i_y_train_time, i_X_valid_time, i_y_valid_time, args["nb_batch"], args["nb_epochs"], callbacks, args["train_verbose"])
                # prediction
                best_model = load_model(file_path)
                prediction = best_model.predict(X_test_time)
                y_pred_test_time.append(prediction)
            
            # aggregate model prediction in each data subset
            y_pred_test_time = np.mean(np.array(y_pred_test_time), axis=0)

            # save information for prediction performance
            save_prediction_plot(y_test_time, y_pred_test_time, write_result_out_dir)
            save_yy_plot(y_test_time, y_pred_test_time, write_result_out_dir)
            accuracy = mse(y_test_time, y_pred_test_time)
            with open(path.join(write_result_out_dir, 'log.txt'), 'w') as f:
                f.write('accuracy : {:.6f}\n'.format(accuracy))
                f.write('=' * 65 + '\n')
            
            keras.backend.clear_session()
            print('\n' * 2 + '-' * 140 + '\n' * 2)

    elif args["train_mode"] == 'noise-injection':
        for target in listdir('dataset/target'):
        
            # make output directory
            write_result_out_dir = path.join(write_out_dir, target)
            makedirs(write_result_out_dir, exist_ok=True)

            # skip source dataset without pickle file
            data_dir_path = path.join('dataset', 'target', target)
            if not path.exists(f'{data_dir_path}/X_train.pkl'): continue
            
            # load dataset
            X_train_time, y_train_time, X_test_time, y_test_time = \
                load_dataset(data_dir_path, time_window)
            X_train_time, y_train_time, X_valid_time, y_valid_time = \
                split_dataset(X_train_time, y_train_time, ratio=args["train_ratio"])

            # construct the model
            file_path = path.join(write_result_out_dir, 'best_model.hdf5')
            callbacks = make_callbacks(file_path)
            input_shape = (X_train_time.shape[1], X_train_time.shape[2])  # x_train.shape[2] is number of variable
            model = regressor(input_shape, args["gpu"], write_result_out_dir, noise=args["noise_var"])
            
            # train the model
            print(f'\nTarget dataset : {target}')
            print(f'\nTarget dataset shape : {X_train_time.shape}')
            model.fit(X_train_time, y_train_time, X_valid_time, y_valid_time, args["nb_batch"], args["nb_epochs"], callbacks, args["train_verbose"])
            
            # prediction and saving
            best_model = load_model(file_path)
            y_pred_test_time = best_model.predict(X_test_time)
            save(model, y_test_time, y_pred_test_time, write_result_out_dir)
    
            keras.backend.clear_session()
            print('\n' * 2 + '-' * 140 + '\n' * 2)

    # make results easy to see
    elif args["train_mode"] == 'score':
        metrics(write_out_dir)


if __name__ == '__main__':
    main()

import os
import sys
import copy
import argparse

import torch
from torch import optim
import torch.nn as nn

import mlflow.pytorch
from torch.utils.data import DataLoader
from torchvision.models import resnet18
import torchvision.transforms as T
from pytorch_lightning.metrics.functional import accuracy
import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint

from utils.base import AuxLoss, WeightedLoss, display_mlflow_run_info, l2_regularization, str2bool, fetch_from_mlflow, get_name, data_loader_mean_and_std
from utils.debug import debug
from utils.dataset_utils import k_fold
from utils.augmentation import get_augmentation
from dataset import Subset, get_dataset

from processing.pipeline_numpy import RawProcessingPipeline
from processing.pipeline_torch import append_additive_layer, raw2rgb, RawToRGB, ParametrizedProcessing, NNProcessing

from model import log_tensor, resnet_model, LitModel, TrackImagesCallback

import segmentation_models_pytorch as smp

from utils.ssim import SSIM

# args to set up task
parser = argparse.ArgumentParser(description='classification_task')
parser.add_argument('--tracking_uri', type=str,
                    default='http://deplo-mlflo-1ssxo94f973sj-890390d809901dbf.elb.eu-central-1.amazonaws.com', help='URI of the mlflow server on AWS')
parser.add_argument('--processor_uri', type=str, default=None,
                    help='URI of the processing model (e.g. s3://mlflow-artifacts-821771080529/1/5fa754c566e3466690b1d309a476340f/artifacts/processing-model)')
parser.add_argument('--classifier_uri', type=str, default=None,
                    help='URI of the net (e.g. s3://mlflow-artifacts-821771080529/1/5fa754c566e3466690b1d309a476340f/artifacts/prediction-model)')
parser.add_argument('--state_dict_uri', type=str,
                    default=None, help='URI of the indices you want to load (e.g. s3://mlflow-artifacts-601883093460/7/4326da05aca54107be8c554de0674a14/artifacts/training')

parser.add_argument('--experiment_name', type=str,
                    default='classification learnable pipeline', help='Specify the experiment you are running, e.g. end2end segmentation')
parser.add_argument('--run_name', type=str,
                    default='test run', help='Specify the name of your run')

parser.add_argument('--log_model', type=str2bool, default=True, help='Enables model logging')
parser.add_argument('--save_locally', action='store_true',
                    help='Model will be saved locally if action is taken')   # TODO: bypass mlflow

parser.add_argument('--track_processing', action='store_true',
                    help='Save images after each trasformation of the pipeline for the test set')
parser.add_argument('--track_processing_gradients', action='store_true',
                    help='Save images of gradients after each trasformation of the pipeline for the test set')
parser.add_argument('--track_save_tensors', action='store_true',
                    help='Save the torch tensors after each trasformation of the pipeline for the test set')
parser.add_argument('--track_predictions', action='store_true',
                    help='Save images after each trasformation of the pipeline for the test set + input gradient')
parser.add_argument('--track_n_images', default=5,
                    help='Track the n first elements of dataset. Only used for args.track_processing=True')
parser.add_argument('--track_every_epoch', action='store_true', help='Track images every epoch or once after training')

# args to create dataset
parser.add_argument('--seed', type=int, default=1, help='Global seed')
parser.add_argument('--dataset', type=str, default='Microscopy',
                    choices=['Drone', 'DroneSegmentation', 'Microscopy'], help='Select dataset')

parser.add_argument('--n_splits', type=int, default=1, help='Number of splits used for training')
parser.add_argument('--train_size', type=float, default=0.8, help='Fraction of training points in dataset')

# args for training
parser.add_argument('--lr', type=float, default=1e-5, help='learning rate used for training')
parser.add_argument('--epochs', type=int, default=3, help='numper of epochs')
parser.add_argument('--batch_size', type=int, default=32, help='Training batch size')
parser.add_argument('--augmentation', type=str, default='none',
                    choices=['none', 'weak', 'strong'], help='Applies augmentation to training')
parser.add_argument('--check_val_every_n_epoch', type=int, default=1)

# args to specify the processing
parser.add_argument('--processing_mode', type=str, default='parametrized',
                    choices=['parametrized', 'static', 'neural_network', 'none'],
                    help='Which type of raw to rgb processing should be used')

# args to specify model
parser.add_argument('--classifier_network', type=str, default='ResNet18', choices=['ResNet18', 'ResNet34', 'Resnet50'],
                    help='Type of pretrained network')
parser.add_argument('--classifier_pretrained', action='store_true',
                    help='Whether to use a pre-trained model or not')
parser.add_argument('--smp_encoder', type=str, default='resnet34', help='segmentation models pytorch encoder')

parser.add_argument('--freeze_processor', action='store_true', help='Freeze raw to rgb processing model weights')
parser.add_argument('--freeze_classifier', action='store_true', help='Freeze classification model weights')

# args to specify static pipeline transformations
parser.add_argument('--sp_debayer', type=str, default='bilinear',
                    choices=['bilinear', 'malvar2004', 'menon2007'], help='Specify algorithm used as debayer')
parser.add_argument('--sp_sharpening', type=str, default='sharpening_filter',
                    choices=['sharpening_filter', 'unsharp_masking'], help='Specify algorithm used for sharpening')
parser.add_argument('--sp_denoising', type=str, default='gaussian_denoising',
                    choices=['gaussian_denoising', 'median_denoising', 'fft_denoising'], help='Specify algorithm used for denoising')

# args to choose training mode
parser.add_argument('--adv_training', action='store_true', help='Enable adversarial training')
parser.add_argument('--adv_aux_weight', type=float, default=1, help='Weighting of the adversarial auxilliary loss')
parser.add_argument('--adv_aux_loss', type=str, default='ssim', choices=['l2', 'ssim'],
                    help='Type of adversarial auxilliary regularization loss')
parser.add_argument('--adv_noise_layer', action='store_true', help='Adds an additive layer to Parametrized Processing')
parser.add_argument('--adv_track_differences', action='store_true', help='Save difference to default pipeline')
parser.add_argument('--adv_parameters', choices=['all', 'black_level', 'white_balance',
                                                 'colour_correction', 'gamma_correct', 'sharpening_filter', 'gaussian_blur', 'additive_layer'],
                    help='Target individual parameters for adversarial training.')

parser.add_argument('--cache_downloaded_models', type=str2bool, default=True)

parser.add_argument('--test_run', action='store_true')


args = parser.parse_args()

os.makedirs('results', exist_ok=True)


def run_train(args):

    print(args)

    DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
    training_mode = 'adversarial' if args.adv_training else 'default'

    # set tracking uri, this is the address of the mlflow server where light experimental data will be stored
    mlflow.set_tracking_uri(args.tracking_uri)
    mlflow.set_experiment(args.experiment_name)
    os.environ['AWS_ACCESS_KEY_ID'] = '#TODO: fill in your aws access key id for mlflow server here'
    os.environ['AWS_SECRET_ACCESS_KEY'] = '#TODO: fill in your aws secret access key for mlflow server here'

    dataset = get_dataset(args.dataset)

    print(f'dataset: {type(dataset).__name__}[{len(dataset)}]')
    print(f'task: {dataset.task}')
    print(f'mode: {training_mode} training')
    print(f'# cross-validation subsets: {args.n_splits}')
    pl.seed_everything(args.seed)
    idxs_kfold = k_fold(dataset, n_splits=args.n_splits, seed=args.seed, train_size=args.train_size)

    # start mlflow parent run for k-fold validation (optional)
    with mlflow.start_run(run_name=args.run_name) as parent_run:

        # start mlflow child run
        for k_iter, (train_indices, valid_indices) in enumerate(idxs_kfold):

            print(f'K_fold subset: {k_iter+1}/{args.n_splits}')

            if args.processing_mode == 'static':
                # only needed if processor outputs should be normalized (might help for classifier training / testing against torch pipeline)
                if args.dataset == 'Drone' or args.dataset == 'DroneSegmentation':
                    mean = torch.tensor([0.35, 0.36, 0.35])
                    std = torch.tensor([0.12, 0.11, 0.12])
                elif args.dataset == 'Microscopy':
                    mean = torch.tensor([0.91, 0.84, 0.94])
                    std = torch.tensor([0.08, 0.12, 0.05])

                # numpy pipeline doesn't use torch batched transformations. Transformations are applied individually to dataloader
                dataset.transform = T.Compose([RawProcessingPipeline(
                    camera_parameters=dataset.camera_parameters,
                    debayer=args.sp_debayer,
                    sharpening=args.sp_sharpening,
                    denoising=args.sp_denoising,
                ),
                    T.Normalize(mean, std)
                ])

                processor = nn.Identity()

            # fetch processor from mlflow
            if args.processor_uri is not None and args.processing_mode != 'none':
                print('Fetching processor: ', end='')
                processor = fetch_from_mlflow(args.processor_uri, type='processor',
                                              use_cache=args.cache_downloaded_models)
            else:
                print(f'processing_mode: {args.processing_mode}')
                normalize_mosaic = None   # normalize after raw has been transformed to rgb image via raw2rgb
                # not strictly necessary, but for processing_mode=='none' this will ensure normalized outputs for the classifier
                # and for processing_mode=='neural_network', the processing segmentation model receives normalized inputs
                # could be evaded via an additional batchnorm!
                # XXX
                if args.dataset == 'Microscopy':
                    mosaic_mean = [0.5663, 0.1401, 0.0731]
                    mosaic_std = [0.097, 0.0423, 0.008]
                    normalize_mosaic = T.Normalize(mosaic_mean, mosaic_std)

                # track individual processing steps for visualization
                track_stages = args.track_processing or args.track_processing_gradients
                if args.processing_mode == 'parametrized':
                    processor = ParametrizedProcessing(
                        camera_parameters=dataset.camera_parameters, track_stages=track_stages, batch_norm_output=True)

                elif args.processing_mode == 'neural_network':
                    processor = NNProcessing(track_stages=track_stages,
                                             normalize_mosaic=normalize_mosaic, batch_norm_output=True)
                elif args.processing_mode == 'none':
                    processor = RawToRGB(reduce_size=True, out_channels=3, track_stages=track_stages,
                                         normalize_mosaic=normalize_mosaic)

            if args.classifier_uri:  # fetch classifier from mlflow
                print('Fetching classifier: ', end='')
                classifier = fetch_from_mlflow(args.classifier_uri, type='classifier',
                                               use_cache=args.cache_downloaded_models)
            else:
                if dataset.task == 'classification':
                    classifier = resnet_model(
                        model=args.classifier_network,
                        pretrained=args.classifier_pretrained,
                        in_channels=3,
                        fc_out_features=len(dataset.classes)
                    )
                else:
                    classifier = smp.UnetPlusPlus(
                        encoder_name=args.smp_encoder,
                        encoder_depth=5,
                        encoder_weights='imagenet',
                        in_channels=3,
                        classes=1,
                        activation=None,
                    )

            if args.freeze_processor and len(list(iter(processor.parameters()))) == 0:
                print('Note: freezing processor without parameters.')
            assert not (args.freeze_processor and args.freeze_classifier), 'Likely no parameters to train.'

            if dataset.task == 'classification':
                loss = nn.CrossEntropyLoss()
                metrics = [accuracy]
            else:
                # loss = utils.base.smp_get_loss(args.smp_loss)    # XXX: add other losses to args.smp_loss
                loss = smp.losses.DiceLoss(mode='binary', from_logits=True)
                metrics = [smp.utils.metrics.IoU()]

            loss_aux = None

            if args.adv_training:   # setup for failure mode search

                assert args.processing_mode == 'parametrized', f"Processing mode ({args.processing_mode}) should be set to 'parametrized' for adversarial training"
                assert args.freeze_classifier, 'Classifier should be frozen for adversarial training'
                assert not args.freeze_processor, 'Processor should not be frozen for adversarial training'

                # copy, so that regularization in rgb space between adversarial and original processor can be computed
                processor_default = copy.deepcopy(processor)
                processor_default.track_stages = args.track_processing
                processor_default.eval()
                processor_default.to(DEVICE)

                for p in processor_default.parameters():
                    p.requires_grad = False

                if args.adv_noise_layer:    # optional additional "noise" layer in processor
                    append_additive_layer(processor)

                if args.adv_aux_loss == 'l2':
                    regularization = l2_regularization
                elif args.adv_aux_loss == 'ssim':
                    regularization = SSIM(window_size=11)
                else:
                    NotImplementedError(args.adv_aux_loss)

                loss = WeightedLoss(loss=loss, weight=-1)

                loss_aux = AuxLoss(
                    loss_aux=regularization,
                    processor_adv=processor,
                    processor_default=processor_default,
                    weight=args.adv_aux_weight,
                )

            augmentation = get_augmentation(args.augmentation)

            model = LitModel(
                classifier=classifier,
                processor=processor,
                loss=loss,
                lr=args.lr,
                loss_aux=loss_aux,
                adv_training=args.adv_training,
                adv_parameters=args.adv_parameters,
                metrics=metrics,
                augmentation=augmentation,
                is_segmentation_task=dataset.task == 'segmentation',
                freeze_classifier=args.freeze_classifier,
                freeze_processor=args.freeze_processor,
            )

            state_dict = vars(args).copy()

            # get train_set_dict
            if args.state_dict_uri:
                state_dict = mlflow.pytorch.load_state_dict(args.state_dict_uri)
                train_indices = state_dict['train_indices']
                valid_indices = state_dict['valid_indices']

            track_indices = list(range(args.track_n_images))

            if dataset.task == 'classification':
                state_dict['classes'] = dataset.classes
            state_dict['device'] = DEVICE
            state_dict['train_indices'] = train_indices
            state_dict['valid_indices'] = valid_indices
            state_dict['elements in train set'] = len(train_indices)
            state_dict['elements in test set'] = len(valid_indices)

            if args.test_run:
                train_indices = train_indices[:args.batch_size]
                valid_indices = valid_indices[:args.batch_size]

            train_set = Subset(dataset, indices=train_indices)
            valid_set = Subset(dataset, indices=valid_indices)
            track_set = Subset(dataset, indices=track_indices)

            train_loader = DataLoader(train_set, batch_size=args.batch_size, num_workers=16, shuffle=True)
            valid_loader = DataLoader(valid_set, batch_size=args.batch_size, num_workers=16, shuffle=False)
            track_loader = DataLoader(track_set, batch_size=args.batch_size, num_workers=16, shuffle=False)

            with mlflow.start_run(run_name=f"{args.run_name}_{k_iter}", nested=True) as child_run:

                if k_iter == 0:
                    display_mlflow_run_info(child_run)

                mlflow.pytorch.log_state_dict(state_dict, artifact_path=None)

                hparams = {
                    'dataset': args.dataset,
                    'processing_mode': args.processing_mode,
                    'training_mode': training_mode,
                }
                if training_mode == 'adversarial':
                    hparams['adv_aux_weight'] = args.adv_aux_weight
                    hparams['adv_aux_loss'] = args.adv_aux_loss

                mlflow.log_params(hparams)

                with open('results/state_dict.txt', 'w') as f:
                    f.write('python ' + ' '.join(sys.argv) + '\n')
                    f.write('\n'.join([f'{k}={v}' for k, v in state_dict.items()]))
                mlflow.log_artifact('results/state_dict.txt', artifact_path=None)

                mlf_logger = pl.loggers.MLFlowLogger(experiment_name=args.experiment_name,
                                                     tracking_uri=args.tracking_uri,)
                mlf_logger._run_id = child_run.info.run_id

                reference_processor = processor_default if args.adv_training and args.adv_track_differences else None

                callbacks = []
                if args.track_processing:
                    callbacks += [TrackImagesCallback(track_loader,
                                                      reference_processor,
                                                      track_every_epoch=args.track_every_epoch,
                                                      track_processing=args.track_processing,
                                                      track_gradients=args.track_processing_gradients,
                                                      track_predictions=args.track_predictions,
                                                      save_tensors=args.track_save_tensors)]

                trainer = pl.Trainer(
                    gpus=1 if DEVICE == 'cuda' else 0,
                    min_epochs=args.epochs,
                    max_epochs=args.epochs,
                    logger=mlf_logger,
                    callbacks=callbacks,
                    check_val_every_n_epoch=args.check_val_every_n_epoch,
                )

                if args.log_model:
                    mlflow.pytorch.autolog(log_every_n_epoch=10)
                    print(f'model_uri="{mlflow.get_artifact_uri()}/model"')

                t = trainer.fit(
                    model,
                    train_dataloader=train_loader,
                    val_dataloaders=valid_loader,
                )

    globals().update(locals())  # for convenient access

    return model


if __name__ == '__main__':
    model = run_train(args)

import random
import time
import warnings
import sys
import argparse
import shutil

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.optim import SGD
from torch.optim.lr_scheduler import LambdaLR, MultiStepLR
from torch.utils.data import DataLoader
from torchvision.transforms import Compose, ToPILImage
import torch.nn.functional as F

#sys.path.append('../../..')
from uda.model.regda_4 import PoseResNet as RegDAPoseResNet, \
    PseudoLabelGenerator, RegressionDisparity4, PoseResNet3 as RegDAPoseResNet3, PoseResNet2 as RegDAPoseResNet2, RegressionDisparity3

from uda.model.regda_7 import PoseResNetx9 as RegDAPoseResNetx1, PoseResNetx10 as RegDAPoseResNetx2, RegressionDisparityx1, RegressionDisparityx5, PseudoLabelGenerator03, PseudoLabelGenerator01, refineNet3, RegressionDisparity, RegressionDisparityx6

import uda.model as models
from uda.model.pose_resnet2 import Upsampling, PoseResNet
from uda.model.loss import JointsKLLoss, update_ema_variables5, loss3
import uda.dataset as datasets
import uda.dataset.keypoint_detection as T
from utils import Denormalize
from utils.data import ForeverDataIterator
from utils.meter import AverageMeter, ProgressMeter, AverageMeterDict
from utils.keypoint_detection import accuracy, compute_uv_from_heatmaps
from utils.logger import CompleteLogger

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

global_step = 0
def main(args: argparse.Namespace):
    logger = CompleteLogger(args.log, args.phase)
    print(args)
    global global_step

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    cudnn.benchmark = True

    # Data loading code
    normalize = T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    train_transform = T.Compose([
        T.RandomRotation(args.rotation),
        T.RandomResizedCrop(size=args.image_size, scale=args.resize_scale),
        T.ColorJitter(brightness=0.25, contrast=0.25, saturation=0.25),
        T.GaussianBlur(),
        T.ToTensor(),
        normalize
    ])
    
    
    val_transform = T.Compose([
        T.Resize(args.image_size),
        T.ToTensor(),
        normalize
    ])
    image_size = (args.image_size, args.image_size)
    heatmap_size = (args.heatmap_size, args.heatmap_size)
    source_dataset = datasets.__dict__[args.source]
    train_source_dataset = source_dataset(root=args.source_root, transforms=train_transform,
                                          image_size=image_size, heatmap_size=heatmap_size)
    train_source_loader = DataLoader(train_source_dataset, batch_size=args.batch_size,
                                     shuffle=True, num_workers=args.workers, pin_memory=True, drop_last=True)
    val_source_dataset = source_dataset(root=args.source_root, split='test', transforms=val_transform,
                                        image_size=image_size, heatmap_size=heatmap_size)
    val_source_loader = DataLoader(val_source_dataset, batch_size=args.batch_size, shuffle=False, pin_memory=True)

    target_dataset = datasets.__dict__[args.target]
    train_target_dataset = target_dataset(root=args.target_root, transforms=train_transform,
                                          image_size=image_size, heatmap_size=heatmap_size)
    train_target_loader = DataLoader(train_target_dataset, batch_size=args.batch_size,
                                     shuffle=True, num_workers=args.workers, pin_memory=True, drop_last=True)
                                                                   
                                     
    val_target_dataset = target_dataset(root=args.target_root, split='test', transforms=val_transform,
                                        image_size=image_size, heatmap_size=heatmap_size)
    val_target_loader = DataLoader(val_target_dataset, batch_size=args.batch_size, shuffle=False, pin_memory=True)

    print("Source train:", len(train_source_loader))
    print("Target train:", len(train_target_loader))
    print("Source test:", len(val_source_loader))
    print("Target test:", len(val_target_loader))

    train_source_iter = ForeverDataIterator(train_source_loader)
    train_target_iter = ForeverDataIterator(train_target_loader)

    # create model
    def creat_ema(model):

        backbone = models.__dict__[args.arch](pretrained=True)
   #     backbone2 = models.__dict__[args.arch2]()
        upsampling = Upsampling(backbone.out_features)
        num_keypoints = train_source_dataset.num_keypoints
        model_ema = RegDAPoseResNetx2(backbone, upsampling, 256, num_keypoints, num_head_layers=args.num_head_layers, finetune=True).to(device)
        # pretrained_dict = torch.load(args.pretrain01, map_location='cpu')['model']
        # model.load_state_dict(pretrained_dict, strict=False)
      
        #for param in model.parameters():
        #    param.detach_()

        for param_main, param_ema in zip(model.parameters(), model_ema.parameters()):
                param_ema.data.copy_(param_main.data)  # initialize
                param_ema.requires_grad = False  # not update by gradient

        return model_ema



    backbone = models.__dict__[args.arch](pretrained=True)
  #  backbone2 = models.__dict__[args.arch2]()
    upsampling = Upsampling(backbone.out_features)
    num_keypoints = train_source_dataset.num_keypoints
    model = RegDAPoseResNetx1(backbone, upsampling, 256, num_keypoints, num_head_layers=args.num_head_layers, finetune=True).to(device)
    model_ema = creat_ema(model)

    # define loss function
    criterion = JointsKLLoss()
    pseudo_label_generator03 = PseudoLabelGenerator03(num_keypoints)
    pseudo_label_generator01 = PseudoLabelGenerator01(num_keypoints)
    pseudo_label_generator = PseudoLabelGenerator(num_keypoints, args.heatmap_size, args.heatmap_size)
    regression_disparity = RegressionDisparityx6(pseudo_label_generator, JointsKLLoss(epsilon=1e-7))
    regression_disparity2 = RegressionDisparityx5(pseudo_label_generator03, JointsKLLoss(epsilon=1e-7))
    regression_disparity1 = RegressionDisparityx1(pseudo_label_generator01, JointsKLLoss(epsilon=1e-7))


    # define optimizer and lr scheduler
    optimizer_f = SGD([
        {'params': backbone.parameters(), 'lr': 0.1},
        {'params': upsampling.parameters(), 'lr': 0.1},
    ], lr=0.1, momentum=args.momentum, weight_decay=args.wd, nesterov=True)
    optimizer_h = SGD(model.head.parameters(), lr=0.1, momentum=args.momentum, weight_decay=args.wd, nesterov=True)
    optimizer_h_adv = SGD(model.head_adv.parameters(), lr=0.1, momentum=args.momentum, weight_decay=args.wd, nesterov=True)
    optimizer_h_adv2 = SGD(model.head_adv2.parameters(), lr=0.1, momentum=args.momentum, weight_decay=args.wd, nesterov=True)
    optimizer_h_adv3 = SGD(model.head_adv3.parameters(), lr=0.1, momentum=args.momentum, weight_decay=args.wd, nesterov=True)
    lr_decay_function = lambda x: args.lr * (1. + args.lr_gamma * float(x)) ** (-args.lr_decay)
    lr_scheduler_f = LambdaLR(optimizer_f, lr_decay_function)
    lr_scheduler_h = LambdaLR(optimizer_h, lr_decay_function)
    lr_scheduler_h_adv = LambdaLR(optimizer_h_adv, lr_decay_function)
    lr_scheduler_h_adv2 = LambdaLR(optimizer_h_adv2, lr_decay_function)
    lr_scheduler_h_adv3 = LambdaLR(optimizer_h_adv3, lr_decay_function)
    start_epoch = 0

    if args.checkpoint is None:
        if args.pretrain is None:
            # first pretrain the backbone and upsampling
            print("Pretraining the model on source domain.")
            args.pretrain = logger.get_checkpoint_path('pretrain')
            pretrained_model = PoseResNet(backbone, upsampling, 256, num_keypoints, True).to(device)
            optimizer = SGD(pretrained_model.get_parameters(lr=args.lr), momentum=args.momentum, weight_decay=args.wd, nesterov=True)
            lr_scheduler = MultiStepLR(optimizer, args.lr_step, args.lr_factor)
            best_acc = 0
            for epoch in range(args.pretrain_epochs):
                lr_scheduler.step()
                print(lr_scheduler.get_lr())

                pretrain(train_source_iter, pretrained_model, criterion, optimizer, epoch, args)
                source_val_acc = validate(val_source_loader, pretrained_model, criterion, None, args)

                # remember best acc and save checkpoint
                if source_val_acc['all'] > best_acc:
                    best_acc = source_val_acc['all']
                    torch.save(
                        {
                            'model': pretrained_model.state_dict()
                        }, args.pretrain
                    )
                print("Source: {} best: {}".format(source_val_acc['all'], best_acc))

        # load from the pretrained checkpoint
        pretrained_dict = torch.load(args.pretrain, map_location='cpu')['model']
        model_dict = model.state_dict()
        # remove keys from pretrained dict that doesn't appear in model dict
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        model.load_state_dict(pretrained_dict, strict=False)
        model_ema.load_state_dict(pretrained_dict, strict=False)
    else:
        # optionally resume from a checkpoint
        checkpoint = torch.load(args.checkpoint, map_location='cpu')
        model.load_state_dict(checkpoint['model'])
        model_ema.load_state_dict(checkpoint['model'])
        optimizer_f.load_state_dict(checkpoint['optimizer_f'])
        optimizer_h.load_state_dict(checkpoint['optimizer_h'])
        optimizer_h_adv.load_state_dict(checkpoint['optimizer_h_adv'])
        lr_scheduler_f.load_state_dict(checkpoint['lr_scheduler_f'])
        lr_scheduler_h.load_state_dict(checkpoint['lr_scheduler_h'])
        lr_scheduler_h_adv.load_state_dict(checkpoint['lr_scheduler_h_adv'])
        start_epoch = checkpoint['epoch'] + 1

    # define visualization function
    tensor_to_image = Compose([
        Denormalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ToPILImage()
    ])

    def visualize(image, keypoint2d, name, heatmaps=None):
        """
        Args:
            image (tensor): image in shape 3 x H x W
            keypoint2d (tensor): keypoints in shape K x 2
            name: name of the saving image
        """
        train_source_dataset.visualize(tensor_to_image(image),
                                       keypoint2d, logger.get_image_path("{}.jpg".format(name)))


    # evaluate on validation set
    source_val_acc = validate(val_source_loader, model, criterion, None, args)
    target_val_acc = validate(val_target_loader, model, criterion, visualize, args)
    print("Source: {:4.3f} Target: {:4.3f}".format(source_val_acc['all'], target_val_acc['all']))
    for name, acc in target_val_acc.items():
         print("{}: {:4.3f}".format(name, acc))
    return
    logger.close()


def pretrain(train_source_iter, model, criterion, optimizer,
             epoch: int, args: argparse.Namespace):
    batch_time = AverageMeter('Time', ':4.2f')
    data_time = AverageMeter('Data', ':3.1f')
    losses_s = AverageMeter('Loss (s)', ":.2e")
    acc_s = AverageMeter("Acc (s)", ":3.2f")

    progress = ProgressMeter(
        args.iters_per_epoch,
        [batch_time, data_time, losses_s, acc_s],
        prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()

    end = time.time()
    for i in range(args.iters_per_epoch):
        optimizer.zero_grad()

        x_s, label_s, weight_s, meta_s = next(train_source_iter)

        x_s = x_s.to(device)
        label_s = label_s.to(device)
        weight_s = weight_s.to(device)

        # measure data loading time
        data_time.update(time.time() - end)

        # compute output
        y_s = model(x_s)
        loss_s = criterion(y_s, label_s, weight_s)

        # compute gradient and do SGD step
        loss_s.backward()
        optimizer.step()

        # measure accuracy and record loss
        _, avg_acc_s, cnt_s, pred_s = accuracy(y_s.detach().cpu().numpy(),
                                               label_s.detach().cpu().numpy())
        acc_s.update(avg_acc_s, cnt_s)
        losses_s.update(loss_s, cnt_s)

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.display(i)


def train(train_source_iter, train_target_iter, model, model_ema, criterion, regression_disparity, regression_disparity2,regression_disparity1,
          optimizer_f, optimizer_h, optimizer_h_adv, optimizer_h_adv2, optimizer_h_adv3, lr_scheduler_f, lr_scheduler_h,                                            lr_scheduler_h_adv, lr_scheduler_h_adv2, lr_scheduler_h_adv3, epoch: int, visualize, args: argparse.Namespace):
    batch_time = AverageMeter('Time', ':4.2f')
    data_time = AverageMeter('Data', ':3.1f')
    losses_s = AverageMeter('Loss (s)', ":.2e")
    losses_gf = AverageMeter('Loss (t, false)', ":.2e")
    losses_gt = AverageMeter('Loss (t, truth)', ":.2e")
    acc_s = AverageMeter("Acc (s)", ":3.2f")
    acc_t = AverageMeter("Acc (t)", ":3.2f")
    acc_s_adv = AverageMeter("Acc (s, adv)", ":3.2f")
    acc_t_adv = AverageMeter("Acc (t, adv)", ":3.2f")
    global global_step

    progress = ProgressMeter(
        args.iters_per_epoch,
        [batch_time, data_time, losses_s, losses_gf, losses_gt, acc_s, acc_t, acc_s_adv, acc_t_adv],
        prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()
 #   model_ema.eval()
    end = time.time()

    m = 0.01 * epoch
    if epoch > 30:
       m = 0.3

    for i in range(args.iters_per_epoch):
        x_s, label_s, weight_s, meta_s = next(train_source_iter)
        x_t, label_t, weight_t, meta_t = next(train_target_iter)

        x_s = x_s.to(device)
        label_s = label_s.to(device)
        weight_s = weight_s.to(device)

        x_t = x_t.to(device)
        x_t_ema = meta_t['image_ema'].to(device)
        label_t = label_t.to(device)
        weight_t = weight_t.to(device)

        # measure data loading time
        data_time.update(time.time() - end)

        # Step A train all networks to minimize loss on source domain
        optimizer_f.zero_grad()
        optimizer_h.zero_grad()
        optimizer_h_adv.zero_grad()
        optimizer_h_adv2.zero_grad()
        optimizer_h_adv3.zero_grad()

        tem = None

        y_s, y_s_adv, y_s_adv2, y_s_adv3, f_s = model(x_s)
        
        


        loss_s = 2 * criterion(y_s, label_s, weight_s) + \
                 4 * regression_disparity2(y_s, y_s_adv2, tem, weight_s, mode='min') + \
                 4 * regression_disparity(y_s, y_s_adv, tem, weight_s, mode='min') + \
                 4 * regression_disparity1(y_s, y_s_adv3, weight_s, mode='min')


                 
        loss_s.backward()
        optimizer_f.step()
        optimizer_h.step()
        optimizer_h_adv.step()
        optimizer_h_adv2.step()
        optimizer_h_adv3.step()
        
        
        # Step B train adv regressor to maximize regression disparity
#        optimizer_h.zero_grad()
        optimizer_h_adv.zero_grad()
        optimizer_h_adv2.zero_grad()
        optimizer_h_adv3.zero_grad()
        y_t, y_t_adv, y_t_adv2, y_t_adv3, f_t = model(x_t)
        
        
        loss1 = args.trade_off * regression_disparity1(y_t, y_t_adv3, weight_t, mode='max')
        
        upsample = nn.Upsample(size=64, mode='bilinear')
        target = upsample(y_t_adv3.detach())
        
        upsample1 = nn.Upsample(size=64, mode='bilinear')
        target1 = upsample1(y_t_adv2.detach())
        
        upsample0 = nn.Upsample(size=32, mode='bilinear')
        target0 = upsample0(y_t_adv3.detach())
        
     #   print(target.shape)
     #   print(target0.shape)
     #   print(y_t_adv.shape)
     #   print(y_t_adv2.shape)
        
        target5 = 0.5 * target + target1
        
        loss2 = args.trade_off * regression_disparity(y_t, y_t_adv, target5, weight_t, mode='max')
        
        loss3 = args.trade_off * regression_disparity2(y_t, y_t_adv2, target0, weight_t, mode='max') 
        
        loss_ground_false =  0.3 * loss1 + 1 * loss2 + 0.3 * loss3
                            
                            
        loss_ground_false.backward()
        optimizer_h_adv2.step()
        optimizer_h_adv.step()
        optimizer_h_adv3.step()
 #       optimizer_h.step()

        # Step C train feature extractor to minimize regression disparity
        optimizer_f.zero_grad()
        y_t, y_t_adv, y_t_adv2, y_t_adv3, f_t = model(x_t)
        
        loss1 = args.trade_off * regression_disparity2(y_t, y_t_adv2, tem, weight_t, mode='min') 
        loss2 = args.trade_off * regression_disparity(y_t, y_t_adv, tem, weight_t, mode='min')
     #   loss3 = args.trade_off * regression_disparity1(y_t, y_t_adv3, weight_t, mode='min') 
        
        loss_ground_truth = 0.3 * loss1 + 1 * loss2
        
        loss_ground_truth.backward()
        optimizer_f.step()

        # do update step
        model.step()
        lr_scheduler_f.step()
        lr_scheduler_h.step()
        lr_scheduler_h_adv.step()
        lr_scheduler_h_adv2.step()
        lr_scheduler_h_adv3.step()

        global_step += 1
    #    update_ema_variables5(model, model_ema, args.ema_decay)

        # measure accuracy and record loss
        _, avg_acc_s, cnt_s, pred_s = accuracy(y_s.detach().cpu().numpy(),
                                               label_s.detach().cpu().numpy())
        acc_s.update(avg_acc_s, cnt_s)
        _, avg_acc_t, cnt_t, pred_t = accuracy(y_t.detach().cpu().numpy(),
                                               label_t.detach().cpu().numpy())
        acc_t.update(avg_acc_t, cnt_t)
        _, avg_acc_s_adv, cnt_s_adv, pred_s_adv = accuracy(y_s_adv.detach().cpu().numpy(),
                                               label_s.detach().cpu().numpy())
        acc_s_adv.update(avg_acc_s_adv, cnt_s)
        _, avg_acc_t_adv, cnt_t_adv, pred_t_adv = accuracy(y_t_adv.detach().cpu().numpy(),
                                               label_t.detach().cpu().numpy())
        acc_t_adv.update(avg_acc_t_adv, cnt_t)
        losses_s.update(loss_s, cnt_s)
        losses_gf.update(loss_ground_false, cnt_s)
        losses_gt.update(loss_ground_truth, cnt_s)

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.display(i)
            if visualize is not None:
                visualize(x_s[0], pred_s[0] * args.image_size / args.heatmap_size, "source_{}_pred".format(i))
                visualize(x_s[0], meta_s['keypoint2d'][0], "source_{}_label".format(i))
                visualize(x_t[0], pred_t[0] * args.image_size / args.heatmap_size, "target_{}_pred".format(i))
                visualize(x_t[0], meta_t['keypoint2d'][0], "target_{}_label".format(i))
                visualize(x_s[0], pred_s_adv[0] * args.image_size / args.heatmap_size, "source_adv_{}_pred".format(i))
                visualize(x_t[0], pred_t_adv[0] * args.image_size / args.heatmap_size, "target_adv_{}_pred".format(i))


def validate(val_loader, model, criterion, visualize, args: argparse.Namespace):
    batch_time = AverageMeter('Time', ':6.3f')
    losses = AverageMeter('Loss', ':.2e')
    acc = AverageMeterDict(val_loader.dataset.keypoints_group.keys(), ":3.2f")
    progress = ProgressMeter(
        len(val_loader),
        [batch_time, losses, acc['all']],
        prefix='Test: ')

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        end = time.time()
        for i, (x, label, weight, meta) in enumerate(val_loader):
            x = x.to(device)
            label = label.to(device)
            weight = weight.to(device)

            # compute output
            y = model(x)
            loss = criterion(y, label, weight)

            # measure accuracy and record loss
            losses.update(loss.item(), x.size(0))
            acc_per_points, avg_acc, cnt, pred = accuracy(y.cpu().numpy(),
                                                          label.cpu().numpy())

            group_acc = val_loader.dataset.group_accuracy(acc_per_points)
            acc.update(group_acc, x.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                progress.display(i)
                if visualize is not None:
                    visualize(x[0], pred[0] * args.image_size / args.heatmap_size, "val_{}_pred.jpg".format(i))
                    visualize(x[0], meta['keypoint2d'][0], "val_{}_label.jpg".format(i))

    return acc.average()


def validate2(val_loader, model, criterion, visualize, args: argparse.Namespace):
    batch_time = AverageMeter('Time', ':6.3f')
    losses = AverageMeter('Loss', ':.2e')
    acc = AverageMeterDict(val_loader.dataset.keypoints_group.keys(), ":3.2f")
    progress = ProgressMeter(
        len(val_loader),
        [batch_time, losses, acc['all']],
        prefix='Test: ')

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        end = time.time()
        for i, (x, label, weight, meta) in enumerate(val_loader):
            x = x.to(device)
            label = label.to(device)
            weight = weight.to(device)

            # compute output
            y, y_adv, y_adv2, y_adv3, f = model(x)
            loss = criterion(y, label, weight)

            # measure accuracy and record loss
            losses.update(loss.item(), x.size(0))
            acc_per_points, avg_acc, cnt, pred = accuracy(y.cpu().numpy(),
                                                          label.cpu().numpy())

            group_acc = val_loader.dataset.group_accuracy(acc_per_points)
            acc.update(group_acc, x.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                progress.display(i)
                if visualize is not None:
                    visualize(x[0], pred[0] * args.image_size / args.heatmap_size, "val_{}_pred.jpg".format(i))
                    visualize(x[0], meta['keypoint2d'][0], "val_{}_label.jpg".format(i))

    return acc.average()

def update_ema_variables(model, ema_model, alpha, global_step):
        # Use the true average until the exponential average is more correct
        alpha = min(1 - 1 / (global_step + 1), alpha)
        for ema_param, param in zip(ema_model.parameters(), model.parameters()):
            # ema_param.data.mul_(alpha).add_(1 - alpha, param.data)
            ema_param.data.mul_(alpha).add_(param.data, alpha=1 - alpha)
            
            

if __name__ == '__main__':
    architecture_names = sorted(
        name for name in models.__dict__
        if name.islower() and not name.startswith("__")
        and callable(models.__dict__[name])
    )
    dataset_names = sorted(
        name for name in datasets.__dict__
        if not name.startswith("__") and callable(datasets.__dict__[name])
    )

    parser = argparse.ArgumentParser(description='Source Only for Keypoint Detection Domain Adaptation')
    # dataset parameters
    parser.add_argument('--source_root', default='data/RHD', help='root path of the source dataset')
    parser.add_argument('target_root', help='root path of the target dataset')
    parser.add_argument('-s', '--source', default='RenderedHandPose', help='source domain(s)')
    parser.add_argument('-t', '--target', help='target domain(s)')
    parser.add_argument('--resize-scale', nargs='+', type=float, default=(0.6, 1.3),
                        help='scale range for the RandomResizeCrop augmentation')
    parser.add_argument('--rotation', type=int, default=180,
                        help='rotation range of the RandomRotation augmentation')
    parser.add_argument('--image-size', type=int, default=256,
                        help='input image size')
    parser.add_argument('--heatmap-size', type=int, default=64,
                        help='output heatmap size')
    # model parameters
    parser.add_argument('-a', '--arch', metavar='ARCH', default='resnet101',
                        choices=architecture_names,
                        help='backbone architecture: ' +
                             ' | '.join(architecture_names) +
                             ' (default: resnet101)')
    parser.add_argument('-a2', '--arch2', metavar='ARCH', default='net_hg',
                        choices=architecture_names,
                        help='backbone architecture: ' +
                             ' | '.join(architecture_names) +
                             ' (default: resnet101)')
    parser.add_argument("--pretrain", type=str, default='models/pretrain_rhd.pth',
                        help="Where restore pretrained model parameters from.")

    parser.add_argument("--ema_model", type=str, default=None,
                        help="Where restore pretrained model parameters from.")
    parser.add_argument("--checkpoint", type=str, default=None,
                        help="where restore model parameters from.")
    parser.add_argument("--resume2", type=str, default=None,
                        help="where restore model parameters from.")
    parser.add_argument('--num-head-layers', type=int, default=2)
    parser.add_argument('--margin', type=float, default=4., help="margin gamma")
    parser.add_argument('--trade-off', default=1., type=float,
                        help='the trade-off hyper-parameter for transfer loss')
    # training parameters
    parser.add_argument('-b', '--batch-size', default=32, type=int,
                        metavar='N',
                        help='mini-batch size (default: 32)')
    parser.add_argument('--lr', '--learning-rate', default=0.01, type=float,
                        metavar='LR', help='initial learning rate', dest='lr')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=0.0001, type=float,
                        metavar='W', help='weight decay (default: 1e-4)')
    parser.add_argument('--lr-gamma', default=0.0001, type=float)
    parser.add_argument('--lr-decay', default=0.75, type=float, help='parameter for lr scheduler')
    parser.add_argument('--lr-step', default=[45, 60], type=tuple, help='parameter for lr scheduler')
    parser.add_argument('--lr-factor', default=0.1, type=float, help='parameter for lr scheduler')
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--pretrain_epochs', default=70, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('--epochs', default=200, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('-i', '--iters-per-epoch', default=500, type=int,
                        help='Number of iterations per epoch')
    parser.add_argument('-p', '--print-freq', default=100, type=int,
                        metavar='N', help='print frequency (default: 100)')
    parser.add_argument('--seed', default=1, type=int,
                        help='seed for initializing training. ')
    parser.add_argument("--log", type=str, default='logs/mt',
                        help="Where to save logs, checkpoints and debugging images.")
    parser.add_argument("--phase", type=str, default='train', choices=['train', 'test'],
                        help="When phase is 'test', only test the model.")
    parser.add_argument('--debug', action="store_true",
                        help='In the debug mode, save images and predictions')
    parser.add_argument('--ema-decay', default=0.999, type=float, metavar='ALPHA',
                        help='ema variable decay rate (default: 0.999)')
    args = parser.parse_args()
    main(args)


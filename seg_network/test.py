import sys
sys.path.insert(0, '..')
sys.path.insert(0, '.')

import torch
import torch.nn.functional as F
from omegaconf import OmegaConf
from PIL import Image
from data_loader import MVTecDRAEMTestDataset, MVTecDRAEMTrainDataset
from torch.utils.data import DataLoader
import numpy as np
from sklearn.metrics import roc_auc_score, average_precision_score
from model_unet import ReconstructiveSubNetwork, DiscriminativeSubNetwork
import os
import cv2
from rec_network.util import instantiate_from_config
from rec_network.models.diffusion.ddim import DDIMSampler

from eval_helper import performances, log_metrics


def write_results_to_file(run_name, image_auc, pixel_auc, image_ap, pixel_ap):
    if not os.path.exists('./outputs/'):
        os.makedirs('./outputs/')

    fin_str = "img_auc," + run_name
    for i in image_auc:
        fin_str += "," + str(np.round(i, 3))
    fin_str += "," + str(np.round(np.mean(image_auc), 3))
    fin_str += "\n"
    fin_str += "pixel_auc," + run_name
    for i in pixel_auc:
        fin_str += "," + str(np.round(i, 3))
    fin_str += "," + str(np.round(np.mean(pixel_auc), 3))
    fin_str += "\n"
    fin_str += "img_ap," + run_name
    for i in image_ap:
        fin_str += "," + str(np.round(i, 3))
    fin_str += "," + str(np.round(np.mean(image_ap), 3))
    fin_str += "\n"
    fin_str += "pixel_ap," + run_name
    for i in pixel_ap:
        fin_str += "," + str(np.round(i, 3))
    fin_str += "," + str(np.round(np.mean(pixel_ap), 3))
    fin_str += "\n"
    fin_str += "--------------------------\n"

    with open("./outputs/results.txt", 'a+') as file:
        file.write(fin_str)


def test(obj_name, data_path, checkpoint_path, base_model_name, epoch='', dataset='mvtec'):

    img_dim = 256
    run_name = base_model_name + "_" + obj_name + '_'

    config = OmegaConf.load(f"./configs/{dataset}.yaml")

    if 'first_stage_config' in config.model.params:
        config.model.params.first_stage_config.params.ckpt_path = os.path.join(f'logs_{dataset}', f"{obj_name}_{dataset}", f'checkpoints_{dataset}', 'last.ckpt')

    model = instantiate_from_config(config.model)

    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    model = model.to(device)
    sampler = DDIMSampler(model)
    if epoch:
        epoch = f'{epoch}_'

    model_seg = DiscriminativeSubNetwork(in_channels=9, out_channels=2)
    model_seg.load_state_dict(torch.load(os.path.join(checkpoint_path, obj_name, run_name+f"{epoch}seg.pckl"), map_location='cuda:0'))
    model_seg.cuda()
    model_seg.eval()

    dataset = MVTecDRAEMTestDataset(os.path.join(data_path, obj_name, "test"), resize_shape=[img_dim, img_dim], dataset=dataset)
    dataloader = DataLoader(dataset, batch_size=1,
                            shuffle=False, num_workers=0)

    total_pixel_scores = np.zeros((img_dim * img_dim * len(dataset)))
    total_gt_pixel_scores = np.zeros((img_dim * img_dim * len(dataset)))
    mask_cnt = 0

    anomaly_score_gt = []
    anomaly_score_prediction = []

    cnt_display = 0

    gt_matrix_pixel=[]
    pred_matrix_pixel=[]

    for i_batch, sample_batched in enumerate(dataloader):
        gray_batch = sample_batched["image"].cuda()

        is_normal = sample_batched["has_anomaly"].detach().numpy()[0, 0]
        anomaly_score_gt.append(is_normal)
        true_mask = sample_batched["mask"]
        true_mask_cv = true_mask.detach().numpy()[0, :, :, :].transpose((1, 2, 0))

        c = model.cond_stage_model.encode(gray_batch)
        c = c.mode()
        noise = torch.randn_like(c)
        t = torch.randint(400, 500, (c.shape[0],), device=device).long()
        c_noisy = model.q_sample(x_start=c, t=t, noise=noise)

        shape = c.shape[1:]
        samples_ddim, _ = sampler.sample(S=10,
                                         conditioning=c, # or conditioning=c_noisy
                                         batch_size=c.shape[0],
                                         shape=shape,
                                         verbose=False)

        gray_rec = model.decode_first_stage(samples_ddim)

        samples_ddim1 = 0.5 * samples_ddim + 0.5 * c
        gray_rec1 = model.decode_first_stage(samples_ddim1)

        joined_in = torch.cat((gray_rec.detach(), gray_rec1.detach(), gray_batch), dim=1)

        out_mask = model_seg(joined_in)
        out_mask_sm = torch.softmax(out_mask, dim=1)

        t_mask = out_mask_sm[:, 1:, :, :]

        outpath = os.path.join('./samples', obj_name, 'test', 'rec_images' + str(cnt_display) + '.jpg')
        sample = gray_rec.cpu().numpy().transpose(0, 2, 3, 1)[0] * 255
        cv2.imwrite(outpath, sample)

        outpath = os.path.join('./samples', obj_name, 'test', 'gt_images' + str(cnt_display) + '.jpg')
        sample = gray_batch.cpu().numpy().transpose(0, 2, 3, 1)[0] * 255
        cv2.imwrite(outpath, sample)

        outpath = os.path.join('./samples', obj_name, 'test', 'out_masks' + str(cnt_display) + '.jpg')
        sample = t_mask[0].detach().cpu().numpy()[0] * 255
        cv2.imwrite(outpath, sample)

        outpath = os.path.join('./samples', obj_name, 'test', 'in_masks' + str(cnt_display) + '.jpg')
        sample = true_mask[0].detach().cpu().numpy()[0] * 255
        cv2.imwrite(outpath, sample)

        heatmap = t_mask[0].detach().cpu().numpy()[0]
        heatmap = heatmap / np.max(heatmap)
        heatmap = np.uint8(255 * heatmap)
        heatmap = cv2.applyColorMap(heatmap, cv2.COLORMAP_JET)
        show = heatmap * 0.5 + gray_batch.detach().cpu().numpy().transpose(0, 2, 3, 1)[0] * 255
        outpath = os.path.join('./samples', obj_name, 'test', 'heatmap' + str(cnt_display) + '.jpg')
        cv2.imwrite(outpath, show)
        cnt_display += 1

        out_mask_cv = out_mask_sm[0, 1, :, :].detach().cpu().numpy()
        out_mask_averaged = torch.nn.functional.avg_pool2d(out_mask_sm[:, 1:, :, :], 21, stride=1,
                                                           padding=21 // 2).cpu().detach().numpy()
        image_score = np.max(out_mask_averaged)
        anomaly_score_prediction.append(image_score)

        flat_true_mask = true_mask_cv.flatten()
        flat_out_mask = out_mask_cv.flatten()
        total_pixel_scores[mask_cnt * img_dim * img_dim:(mask_cnt + 1) * img_dim * img_dim] = flat_out_mask
        total_gt_pixel_scores[mask_cnt * img_dim * img_dim:(mask_cnt + 1) * img_dim * img_dim] = flat_true_mask

        gt_matrix_pixel.extend(true_mask_cv[None, :,:,0].astype(int))
        pred_matrix_pixel.extend(out_mask_cv[None, :])

        mask_cnt += 1
        if mask_cnt > 5:
            break
    
    """
    anomaly_score_prediction = np.array(anomaly_score_prediction)
    anomaly_score_gt = np.array(anomaly_score_gt)
    auroc = roc_auc_score(anomaly_score_gt, anomaly_score_prediction)
    ap = average_precision_score(anomaly_score_gt, anomaly_score_prediction)

    total_gt_pixel_scores = total_gt_pixel_scores.astype(np.uint8)
    total_gt_pixel_scores = total_gt_pixel_scores[:img_dim * img_dim * mask_cnt]
    total_pixel_scores = total_pixel_scores[:img_dim * img_dim * mask_cnt]
    auroc_pixel = roc_auc_score(total_gt_pixel_scores, total_pixel_scores)
    ap_pixel = average_precision_score(total_gt_pixel_scores, total_pixel_scores)

    print(obj_name)
    print("AUC Image:  " + str(auroc))
    print("AP Image:  " + str(ap))
    print("AUC Pixel:  " + str(auroc_pixel))
    print("AP Pixel:  " + str(ap_pixel))

    write_results_to_file(run_name, auroc, auroc_pixel, ap, ap_pixel)
    """
    total_gt_pixel_scores = total_gt_pixel_scores.astype(np.uint8)
    total_gt_pixel_scores = total_gt_pixel_scores[:img_dim * img_dim * mask_cnt]
    total_pixel_scores = total_pixel_scores[:img_dim * img_dim * mask_cnt]
    
    evl_metrics = {'auc': [ {'name': 'max'}, {'name': 'pixel'}, {'name': 'pro'}, {'name': 'appx'}, {'name': 'apsp'}, {'name': 'f1px'}, {'name': 'f1sp'}]}
    print("Gathering final results ...")
    ret_metrics = performances(pred_matrix_pixel, gt_matrix_pixel, evl_metrics, obj_name)
    return ret_metrics


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu_id', action='store', type=int, required=True)
    parser.add_argument('--base_model_name', action='store', type=str, required=True)
    parser.add_argument('--data_path', action='store', type=str, required=True)
    parser.add_argument('--checkpoint_path', action='store', type=str, required=True)
    parser.add_argument('--ema', action='store_true')

    parser.add_argument("--logit_transform", default=False)
    parser.add_argument("--uniform_dequantization", default=False)
    parser.add_argument("--gaussian_dequantization", default=False)
    parser.add_argument("--random_flip", default=True)
    parser.add_argument("--rescaled", default=True)
    parser.add_argument("--sample_type", type=str, default="generalized",
                        help="sampling approach (generalized or ddpm_noisy)")
    parser.add_argument("--skip_type", type=str, default="uniform", help="skip according to (uniform or quadratic)")
    parser.add_argument("--epoch", type=str, default="", help="eval epoch, 100 or empty for last")
    parser.add_argument("--dataset", type=str, default="mvtec", help="dataset to test")

    args = parser.parse_args()

    if args.dataset == 'mvtec':
        obj_list = ['bottle',
                    'capsule',
                    'carpet',
                    'leather',
                    'pill',
                    'transistor',
                    'tile',
                    'cable',
                    'zipper',
                    'toothbrush',
                    'metal_nut',
                    'hazelnut',
                    'screw',
                    'grid',
                    'wood'
                    ]
    if args.dataset == 'visa':
        obj_list = ['candle',
                    'capsules',
                    'cashew',
                    'chewinggum',
                    'fryum',
                    'macaroni1',
                    'macaroni2',
                    'pcb1',
                    'pcb2',
                    'pcb3',
                    'pcb4',
                    'pipe_fryum'
                    ]

    ret_metrics = {}
    for obj_name in obj_list:
        with torch.cuda.device(args.gpu_id):
            # test(obj_name, args.data_path, args.checkpoint_path, args.base_model_name)
            ret_metric = test(obj_name, args.data_path, args.checkpoint_path, args.base_model_name, args.epoch, args.dataset)
            ret_metrics.update(ret_metric)
    
    config = {'auc': [ {'name': 'max'}, {'name': 'pixel'}, {'name': 'pro'}, {'name': 'appx'}, {'name': 'apsp'}, {'name': 'f1px'}, {'name': 'f1sp'}]}
    if config.get("auc", None):
        for metric in config["auc"]:
            evalname = metric["name"]
            evalvalues = [
                ret_metrics["{}_{}_auc".format(clsname, evalname)]
                for clsname in obj_list
            ]
            mean_auc = np.mean(np.array(evalvalues))
            ret_metrics["{}_{}_auc".format("mean", evalname)] = mean_auc
    log_metrics(ret_metrics, config)

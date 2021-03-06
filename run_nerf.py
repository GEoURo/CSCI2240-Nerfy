import os
import imageio
import time
import torch.nn.functional

from tqdm import tqdm, trange
from datetime import datetime

from NeRF import NeRF

from render import *
from argparser import config_parser
from load_blender import load_blender_data

np.random.seed(0)


def run_network(pts, view_dir, model, chunk=1024 * 64, aux_scene_params=None):
    xx = pts
    pts_flatten = pts.view(pts.shape[0] * pts.shape[1], pts.shape[2])  # (N, 64, 3) -> (N * 64, 3)
    view_dir = view_dir.view(view_dir.shape[0], 1, view_dir.shape[1])  # (N, 3) -> (N, 1, 3)
    view_dir = view_dir.repeat(1, xx.shape[1], 1)  # (N, 1, 3) -> (N, 64, 3)
    view_dir = view_dir.view(view_dir.shape[0] * view_dir.shape[1], view_dir.shape[2])  # (N, 64, 3) -> (N * 64, 3)

    outputs_flat = torch.cat([model(pts_flatten[i:i + chunk], view_dir[i:i + chunk], p=aux_scene_params)
                              for i in range(0, pts_flatten.shape[0], chunk)], 0)
    outputs = torch.reshape(outputs_flat, list(pts.shape[:-1]) + [outputs_flat.shape[-1]])
    return outputs


def create_nerf(args, bounding_box=None):
    """
    Instantiate NeRF's MLP model.
    """
    if args.i_embed == 1:
        model = NeRF(StemDepth=1, ColorDepth=3,
                     StemHiddenDim=64, ColorHiddenDim=64,
                     GeoFeatDim=15, RequiresPositionEmbedding=(0,),
                     INGP=True, BoundingBox=bounding_box,
                     Log2TableSize=args.log2_hashmap_size,
                     FinestRes=args.finest_res, nAuxParams=1).to(device)
    else:
        model = NeRF().to(device)

    grad_vars = list(model.parameters())

    model_fine = None

    if args.N_importance > 0:
        if args.i_embed == 1:
            model_fine = NeRF(StemDepth=1, ColorDepth=3,
                              StemHiddenDim=64, ColorHiddenDim=64,
                              GeoFeatDim=15, RequiresPositionEmbedding=(0,),
                              INGP=True, BoundingBox=bounding_box,
                              Log2TableSize=args.log2_hashmap_size,
                              FinestRes=args.finest_res, nAuxParams=1).to(device)
        else:
            model_fine = NeRF().to(device)

        grad_vars += list(model_fine.parameters())

    network_query_fn = lambda inputs, viewdirs, network_fn, aux_scene_params: run_network(
                                                                        inputs, viewdirs, network_fn,
                                                                        chunk=args.netchunk, aux_scene_params=aux_scene_params)

    # Create optimizer
    if args.i_embed == 1:
        optimizer = torch.optim.RAdam(params=grad_vars, lr=args.lrate, betas=(0.9, 0.99))
    else:
        optimizer = torch.optim.Adam(params=grad_vars, lr=args.lrate, betas=(0.9, 0.999))

    start = 0
    basedir = args.basedir
    expname = args.expname

    ##########################

    # Load checkpoints
    if args.ft_path is not None and args.ft_path != 'None':
        ckpts = [args.ft_path]
    else:
        ckpts = [os.path.join(basedir, expname, f) for f in sorted(os.listdir(os.path.join(basedir, expname))) if
                 'tar' in f]

    print('Found ckpts', ckpts)
    if len(ckpts) > 0 and not args.no_reload:
        ckpt_path = ckpts[-1]
        print('Reloading from', ckpt_path)
        ckpt = torch.load(ckpt_path)

        start = ckpt['global_step']
        optimizer.load_state_dict(ckpt['optimizer_state_dict'])

        # Load model
        model.load_state_dict(ckpt['network_fn_state_dict'])
        if model_fine is not None:
            model_fine.load_state_dict(ckpt['network_fine_state_dict'])

    ##########################

    render_kwargs_train = {
        'network_query_fn': network_query_fn,
        'perturb': args.perturb,
        'N_importance': args.N_importance,
        'network_fine': model_fine,
        'N_samples': args.N_samples,
        'network_fn': model,
        'use_viewdirs': args.use_viewdirs,
        'white_bkgd': args.white_bkgd,
        'raw_noise_std': args.raw_noise_std,
    }

    # NDC only good for LLFF-style forward facing data
    if args.dataset_type != 'llff' or args.no_ndc:
        print('Not ndc!')
        render_kwargs_train['ndc'] = False
        render_kwargs_train['lindisp'] = args.lindisp

    render_kwargs_test = {k: render_kwargs_train[k] for k in render_kwargs_train}
    render_kwargs_test['perturb'] = False
    render_kwargs_test['raw_noise_std'] = 0.

    return render_kwargs_train, render_kwargs_test, start, grad_vars, optimizer


def render_images(render_poses, hwf, K, chunk, render_kwargs, gt_imgs=None, savedir=None, render_factor=0, aux_scene_params=None):
    H, W, focal = hwf

    if render_factor != 0:
        # Render down-sampled image for speed
        H = H // render_factor
        W = W // render_factor
        focal = focal / render_factor
        hwf = (H, W, focal)

    rgbs = []
    disps = []
    psnrs = []

    t = time.time()

    # FOR RENDERING VIDEO WITH VARYING LIGHT INTENSITY:
    # sample x in [0.0, 2.0] : abs(sin(x * pi/2))^2
    light_vals = torch.linspace(0.0, 2.0, len(render_poses))
    light_vals = torch.abs(torch.sin(light_vals * 3.14159265/2.0)) ** 2.0

    # FOR RENDERING VIDEO WITH VARYING LIGHT POSITION:
    # ts = torch.linspace(0.0, 2 * 3.141592, len(render_poses))
    # light_poses = torch.stack([torch.cos(ts), torch.sin(ts), torch.ones(len(render_poses)) * 0.25])
    # light_poses[0] = (light_poses[0] + 1.0) / (2.0)
    # light_poses[1] = (light_poses[1] + 1.0) / (2.0)
    # light_poses = light_poses.t()
    # print(light_poses)

    # FOR RENDERING VIDEO WITH VARYING DIFFUSE CHANNEL:
    # ts = torch.linspace(0.0, 2.0, len(render_poses))
    # red = torch.abs(torch.sin(ts * 3.14159265/2.0)) ** 2.0
    # diffuse_vals = torch.stack([torch.ones(len(render_poses)) * 0.5, red, 1-red]).t()

    # FOR RENDERING VIDEO WITH MOVING OBJECT:
    # ts = torch.linspace(0.0, 2 * 3.141592, len(render_poses))
    # obj_poses = torch.stack([torch.cos(ts), torch.sin(ts), torch.ones(len(render_poses)) * 0.012131691])
    # obj_poses[0] = (obj_poses[0] + 1.0) / (2.0)
    # obj_poses[1] = (obj_poses[1] + 1.0) / (2.0)
    # obj_poses = obj_poses.t()
    # print(obj_poses)

    for i, c2w in enumerate(tqdm(render_poses)):
        print(i, time.time() - t)
        t = time.time()
        # if aux_scene_params is None:
        #     aux_scene_param = torch.tensor(0.1)
        # else:
        #     aux_scene_param = aux_scene_params[i]
        aux_scene_param = light_vals[i]
        # aux_scene_param = light_poses[i]
        # aux_scene_param = diffuse_vals[i]
        # aux_scene_param = obj_poses[i]
        rgb, disp, acc, _ = render(H, W, K, chunk=chunk, c2w=c2w[:3, :4], aux_scene_params=aux_scene_param, **render_kwargs)
        rgbs.append(rgb.cpu().numpy())
        disps.append(disp.cpu().numpy())
        if i == 0:
            print(rgb.shape, disp.shape)

        if gt_imgs is not None and render_factor == 0:
            p = -10. * np.log10(np.mean(np.square(rgb.cpu().numpy() - gt_imgs[i])))
            print(p)
            psnrs.append(p)

        if savedir is not None:
            rgb8 = to8b(rgbs[-1])
            filename = os.path.join(savedir, '{:03d}.png'.format(i))
            imageio.imwrite(filename, rgb8)

    rgbs = np.stack(rgbs, 0)
    disps = np.stack(disps, 0)
    if gt_imgs is not None and render_factor == 0:
        print("Avg PSNR over Test set: ", sum(psnrs) / len(psnrs))

    return rgbs, disps


def train():
    parser = config_parser()
    args = parser.parse_args()

    # Load data
    K = None
    if args.dataset_type == 'blender':
        images, poses, render_poses, hwf, i_split, bounding_box, near, far, aux_scene_params = load_blender_data(args.datadir,
                                                                                               args.half_res,
                                                                                               args.testskip,
                                                                                               args.use_aux_params)
        args.bounding_box = bounding_box
        print('Loaded blender', images.shape, render_poses.shape, hwf, args.datadir)
        i_train, i_val, i_test = i_split

        if args.white_bkgd:
            images = images[..., :3] * images[..., -1:] + (1. - images[..., -1:])
        else:
            images = images[..., :3]

    else:
        print('Unknown dataset type', args.dataset_type, 'exiting')
        return

    # Cast intrinsics to right types
    H, W, focal = hwf
    H, W = int(H), int(W)
    hwf = [H, W, focal]

    if K is None:
        K = np.array([
            [focal, 0, 0.5 * W],
            [0, focal, 0.5 * H],
            [0, 0, 1]
        ])

    if args.render_test:
        render_poses = np.array(poses[i_test])

    # Create log dir and copy the config file
    basedir = args.basedir
    if args.i_embed == 1:
        args.expname += "_INGP"
        args.expname += "_log2T" + str(args.log2_hashmap_size)
    args.expname += datetime.now().strftime('_%d_%H_%M')

    expname = args.expname
    print("expname:", expname)

    os.makedirs(os.path.join(basedir, expname), exist_ok=True)
    f = os.path.join(basedir, expname, 'args.txt')
    with open(f, 'w') as file:
        for arg in sorted(vars(args)):
            attr = getattr(args, arg)
            file.write('{} = {}\n'.format(arg, attr))
    if args.config is not None:
        f = os.path.join(basedir, expname, 'config.txt')
        with open(f, 'w') as file:
            file.write(open(args.config, 'r').read())

    # Create nerf model
    # TODO: switch to scene param model
    render_kwargs_train, render_kwargs_test, start, grad_vars, optimizer = create_nerf(args, bounding_box=bounding_box)
    global_step = start

    bds_dict = {
        'near': near,
        'far': far,
    }
    render_kwargs_train.update(bds_dict)
    render_kwargs_test.update(bds_dict)

    # Move testing data to GPU
    render_poses = torch.Tensor(render_poses).to(device)

    # Short circuit if only rendering out from trained model
    if args.render_only:
        print('RENDER ONLY')
        with torch.no_grad():
            if args.render_test:
                # render_test switches to test poses
                images = images[i_test]
            else:
                # Default is smoother render_poses path
                images = None

            testsavedir = os.path.join(basedir, expname,
                                       'renderonly_{}_{:06d}'.format('test' if args.render_test else 'path', start))
            os.makedirs(testsavedir, exist_ok=True)
            print('test poses shape', render_poses.shape)

            rgbs, _ = render_images(render_poses, hwf, K, args.chunk, render_kwargs_test, gt_imgs=images,
                                    savedir=testsavedir, render_factor=args.render_factor)
            print('Done rendering', testsavedir)
            imageio.mimwrite(os.path.join(testsavedir, 'video.mp4'), to8b(rgbs), fps=30, quality=8)

            return

    # Prepare ray batch tensor if batching random rays
    N_rand = args.N_rand

    poses = torch.Tensor(poses).to(device)

    N_iters = args.N_iters + 1
    print('Begin')
    print('TRAIN views are', i_train)
    print('TEST views are', i_test)
    print('VAL views are', i_val)
    print('light intensities', len(aux_scene_params))
    loss_list = []
    psnr_list = []
    time_list = []
    start = start + 1
    for i in trange(start, N_iters):
        time0 = time.time()

        # Random from one image
        img_i = np.random.choice(i_train)
        target = images[img_i]
        target = torch.Tensor(target).to(device)
        pose = poses[img_i, :3, :4]
        aux_scene_params = torch.Tensor(aux_scene_params).to(device)

        # Grab the auxiliary scene param for current image
        aux_scene_param = aux_scene_params[img_i] if args.use_aux_params else None

        rays_o, rays_d = get_rays(H, W, K, torch.Tensor(pose))  # (H, W, 3), (H, W, 3)

        if i < args.precrop_iters:
            dH = int(H // 2 * args.precrop_frac)
            dW = int(W // 2 * args.precrop_frac)
            coords = torch.stack(
                torch.meshgrid(
                    torch.linspace(H // 2 - dH, H // 2 + dH - 1, 2 * dH),
                    torch.linspace(W // 2 - dW, W // 2 + dW - 1, 2 * dW)
                ), -1)
            if i == start:
                print(
                    f"[Config] Center cropping of size {2 * dH} x {2 * dW} is enabled until iter {args.precrop_iters}")
        else:
            coords = torch.stack(torch.meshgrid(torch.linspace(0, H - 1, H), torch.linspace(0, W - 1, W)),
                                 -1)  # (H, W, 2)

        coords = torch.reshape(coords, [-1, 2])  # (H * W, 2)
        select_inds = np.random.choice(coords.shape[0], size=[N_rand], replace=False)  # (N_rand,)
        select_coords = coords[select_inds].long()  # (N_rand, 2)
        rays_o = rays_o[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
        rays_d = rays_d[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
        batch_rays = torch.stack([rays_o, rays_d], 0)
        target_s = target[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)

        #####  Core optimization loop  #####
        rgb, disp, acc, extras = render(H, W, K, chunk=args.chunk, rays=batch_rays,
                                        verbose=i < 10, retraw=True, aux_scene_params=aux_scene_param,
                                        **render_kwargs_train)

        optimizer.zero_grad()
        img_loss = img2mse(rgb, target_s)
        # trans = extras['raw'][..., -1]
        loss = img_loss
        psnr = mse2psnr(img_loss)

        if 'rgb0' in extras:
            img_loss0 = img2mse(extras['rgb0'], target_s)
            loss = loss + img_loss0
            psnr0 = mse2psnr(img_loss0)

        sparsity_loss = args.sparse_loss_weight * (extras["sparsity_loss"].sum() + extras["sparsity_loss0"].sum())
        loss = loss + sparsity_loss

        # add Total Variation loss
        # if args.i_embed==1:
        #     n_levels = render_kwargs_train["embed_fn"].n_levels
        #     min_res = render_kwargs_train["embed_fn"].base_resolution
        #     max_res = render_kwargs_train["embed_fn"].finest_resolution
        #     log2_hashmap_size = render_kwargs_train["embed_fn"].log2_hashmap_size
        #     TV_loss = sum(total_variation_loss(render_kwargs_train["embed_fn"].embeddings[i], \
        #                                       min_res, max_res, \
        #                                       i, log2_hashmap_size, \
        #                                       n_levels=n_levels) for i in range(n_levels))
        #     loss = loss + args.tv_loss_weight * TV_loss
        #     if i>1000:
        #         args.tv_loss_weight = 0.0

        loss.backward()
        optimizer.step()

        # NOTE: IMPORTANT!
        ###   update learning rate   ###
        decay_rate = 0.1
        decay_steps = args.lrate_decay * 1000
        new_lrate = args.lrate * (decay_rate ** (global_step / decay_steps))
        for param_group in optimizer.param_groups:
            param_group['lr'] = new_lrate
        ################################

        t = time.time() - time0
        # print(f"Step: {global_step}, Loss: {loss}, Time: {dt}")
        #####           end            #####

        # Rest is logging
        if i % args.i_weights == 0:
            path = os.path.join(basedir, expname, '{:06d}.tar'.format(i))
            torch.save({
                'global_step': global_step,
                'network_fn_state_dict': render_kwargs_train['network_fn'].state_dict(),
                'network_fine_state_dict': render_kwargs_train['network_fine'].state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
            }, path)
            print('Saved checkpoints at', path)

        if i % args.i_video == 0 and i > 0:
            # Turn on testing mode
            with torch.no_grad():
                rgbs, disps = render_images(render_poses, hwf, K, args.chunk, render_kwargs_test)
            print('Done, saving', rgbs.shape, disps.shape)
            moviebase = os.path.join(basedir, expname, '{}_spiral_{:06d}_'.format(expname, i))
            imageio.mimwrite(moviebase + 'rgb.mp4', to8b(rgbs), fps=30, quality=8)
            imageio.mimwrite(moviebase + 'disp.mp4', to8b(disps / np.max(disps)), fps=30, quality=8)

            # if args.use_viewdirs:
            #     render_kwargs_test['c2w_staticcam'] = render_poses[0][:3,:4]
            #     with torch.no_grad():
            #         rgbs_still, _ = render_path(render_poses, hwf, args.chunk, render_kwargs_test)
            #     render_kwargs_test['c2w_staticcam'] = None
            #     imageio.mimwrite(moviebase + 'rgb_still.mp4', to8b(rgbs_still), fps=30, quality=8)
        if (i % args.i_testset == 0 and i > 0) or i == 100:
            testsavedir = os.path.join(basedir, expname, 'testset_{:06d}'.format(i))
            os.makedirs(testsavedir, exist_ok=True)
            # print('test poses shape', poses[i_test].shape)
            with torch.no_grad():
                test_poses = torch.cat((poses[i_train[0:3]], poses[i_test]), dim=0).to(device)
                test_image = np.concatenate((images[i_train[0:3]], images[i_test]), axis=0)

                # test_poses = torch.cat((test_poses, poses[i_test]), dim=0).to(device)
                # test_image = np.concatenate((test_image, images[i_test]), axis=0)
                # print('test_poses ', test_poses.shape)
                # -0.18 -0.093 0.25
                # diffuse :
                test_aux_scene_params = torch.cat([aux_scene_params[i_train[0:3]], aux_scene_params[i_test]], dim=0)

                # light pos:
                # train_scene_params = aux_scene_params[i_train[0:3]]
                # train_scene_params[2] = torch.tensor([-0.18, -0.093, 0.25])
                # test_aux_scene_params = torch.cat([train_scene_params, aux_scene_params[i_test]], dim=0)
                # print(train_scene_params)
                # 0/0
                # light intensity:
                # test_aux_scene_params = torch.cat([torch.Tensor([0.75, 0.75, 0.2]), torch.Tensor(aux_scene_params[i_test])], dim=0)
                # test_aux_scene_params = torch.cat([test_aux_scene_params, torch.Tensor([0.25, 0.75])], dim=0)
                # print('test aux scene ', len(test_aux_scene_params))
                render_images(test_poses, hwf, K, args.chunk, render_kwargs_test,
                              gt_imgs=test_image, savedir=testsavedir, aux_scene_params=test_aux_scene_params)
            print('Saved test set')

        if i % args.i_print == 0:
            tqdm.write(f"[TRAIN] Iter: {i} Loss: {loss.item()}  PSNR: {psnr.item()}")
            # loss_list.append(loss.item())
            # psnr_list.append(psnr.item())
            # time_list.append(t)
            # loss_psnr_time = {
            #     "losses": loss_list,
            #     "psnr": psnr_list,
            #     "time": time_list
            # }
            # with open(os.path.join(basedir, expname, "loss_vs_time.pkl"), "wb") as fp:
            #     pickle.dump(loss_psnr_time, fp)

        global_step += 1

if __name__ == '__main__':
    torch.set_default_tensor_type('torch.cuda.FloatTensor')

    train()

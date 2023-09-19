import pickle

import einops

from pprint import pprint
from habitat.utils.visualizations.utils import observations_to_image
from habitat_baselines.utils.common import batch_obs, generate_video
from habitat_sim.utils.datasets_download import argparse
from habitat_baselines.utils.info_dict import extract_scalars_from_info

import numpy as np
import random
from hydra.utils import instantiate

from habitat import logger
from habitat.config import read_write
from habitat_baselines.rl.ddppo.ddp_utils import (init_distrib_slurm, get_distrib_size, rank0_only)
from omegaconf import OmegaConf

import torch
import torch.nn.functional as F
import os
import torch.distributed
from torch.nn.parallel import DistributedDataParallel as DDP

from torch.utils.data import DataLoader
from lmnav.common.lr_utils import get_lr_schedule_lambda
from lmnav.common.resumable_random_sampler import ResumableRandomSampler
from lmnav.config.default import get_config
from lmnav.common.utils import all_reduce
from lmnav.config.default_structured_configs import ArtifactConfig

from lmnav.dataset.data_gen import  _init_envs
from lmnav.models import *
from lmnav.dataset.offline_episode_dataset import OfflineEpisodeDataset
from lmnav.processors import *
from lmnav.common.episode_processor import apply_transforms_images, apply_transforms_inputs, construct_subsequences 

from lmnav.common.writer import *
from lmnav.dataset.offline_episode_dataset import *


os.environ["MAGNUM_LOG"] = "quiet"
os.environ["HABITAT_SIM_LOG"] = "quiet"

class BCTrainer:
    
    def __init__(self, config, eval=False, verbose=False):
        self.config = config
        self.verbose = verbose
        self.exp_folder = os.path.join(self.config.exp.root_dir,
                                       self.config.exp.group,
                                       self.config.exp.job_type,
                                       self.config.exp.name)
        self.writer = instantiate(self.config.exp.logger, eval_mode=eval)

    def validate_config(self):
        """
        validates that config parameters are constrained properly
        """ 
        batch_size = self.config.train.batch_size
        minibatch_size = self.config.train.minibatch_size
        num_minibatches = batch_size // minibatch_size
        num_grad_accums = self.config.train.num_grad_accums

        assert batch_size % minibatch_size == 0, 'batch must be evenly partitioned into minibatch sizes'
        assert num_minibatches % num_grad_accums == 0, '# of grad accums must divide num_minibatches equally'

        
    def initialize_eval(self):
        """
        Initializes controller for evaluation process.
        NOTE: distributed eval is not set up here
        """
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.rank = 0
        self.is_distributed = False
        self.eval_dir = os.path.join(self.exp_folder, 'eval')

        self.agent = self.setup_student()
        self.agent.eval()
        
        self.envs, env_spec = _init_envs(self.config)
        
        self.writer.open(self.config)


    def initialize_train(self):
        """
        Initializes distributed controller for DDP, starts data generator process
        """
        self.validate_config()
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        local_rank, world_rank, world_size = get_distrib_size()
        self.is_distributed = world_size > 1 or True

        if self.is_distributed:
            self.device = torch.device(f"cuda:{local_rank}")
            torch.cuda.set_device(self.device)

            # initialize slurm distributed controller
            backend = self.config.habitat_baselines.rl.ddppo.distrib_backend
            print(f"Starting distributed controller using {backend}")
            local_rank, tcp_store = init_distrib_slurm(backend)

            self.rank = local_rank

            if rank0_only():
                logger.info(f"Initialized DDP-BC with {torch.distributed.get_world_size()} workers")

            # update gpu ids for this process
            with read_write(self.config):
                self.config.device = f'cuda:{local_rank}'
                self.config.habitat_baselines.torch_gpu_id = local_rank
                self.config.habitat.simulator.habitat_sim_v0.gpu_device_id = local_rank

                self.config.habitat.seed += (torch.distributed.get_rank() * self.config.habitat_baselines.num_environments)

            random.seed(self.config.habitat.seed)
            np.random.seed(self.config.habitat.seed)
            torch.manual_seed(self.config.habitat.seed)

            self.artifact_store = torch.distributed.PrefixStore("artifacts", tcp_store)

        # set up student
        os.makedirs(self.exp_folder, exist_ok=True)
        os.makedirs(os.path.join(self.exp_folder, 'ckpts'), exist_ok=True)

        self.agent = self.setup_student()

        # set up optimizer
        optim_params = list(filter(lambda p: p.requires_grad, self.agent.parameters()))
        self.optim = torch.optim.Adam(params=[{'params': optim_params, 'lr': self.config.train.lr_schedule.lr}])
        self.lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer=self.optim,
                                                              lr_lambda=[get_lr_schedule_lambda(self.config.train.lr_schedule)])
        # set up writer and scatter all relevant data to worker nodes
        if rank0_only(): 
            self.writer.open(self.config) 
            data_files = self.writer.load_dataset(self.config.train.dataset)
            self.artifact_store.set("data_files", ';'.join(data_files))
        else:
            self.artifact_store.wait(["data_files"])
            data_files = self.artifact_store.get("data_files").decode('utf-8').split(';')

        # set up dataset
        self.dataset = OfflineEpisodeDataset(files=data_files)
        self.sampler = ResumableRandomSampler(self.dataset)
        self.data_loader = DataLoader(self.dataset, batch_size=self.config.train.episodes_per_batch,
                                      collate_fn=lambda x: x, num_workers=1, sampler=self.sampler)

        self.step, self.epoch = 0, 0
        self.cumstats = {
            'step': 0,
            'epoch': 0,
            'metrics/total_frames': 0
        }

        if self.config.exp.resume_id is not None:
            # TODO; update this with wandb maybe?
            artifact_name = f'{self.config.exp.group}-{self.config.exp.job_type}-{self.config.exp.name}'
            artifact_name = artifact_name.replace('+', '_')
            artifact_name = artifact_name.replace('=', '_')

            if rank0_only():
                ckpt_path = self.writer.load_model(ArtifactConfig(name=artifact_name, version='latest', dirpath=None))
                print(f"Loading actor policy {artifact_name} from config: {ckpt_path}")
                self.artifact_store.set("actor_policy_ckpt", ckpt_path)
            else:
                self.artifact_store.wait(["actor_policy_ckpt"])
                ckpt_path = self.artifact_store.get("actor_policy_ckpt").decode('utf-8')
                
            self.load_checkpoint(ckpt_path) 

        print("Starting train!")
        torch.distributed.barrier()


    def setup_student(self):
        model = instantiate(self.config.train.policy)

        self.vis_processor = model.vis_encoder.vis_processor

        agent = model.to(self.device)
        agent.train()

        if self.is_distributed:
            print(f"Setting up DDP on GPU {self.rank}")
            agent = DDP(agent, device_ids=[self.rank])

        num_params = sum([param.numel() for param in agent.parameters()])
        num_trainable_params = sum([param.numel() for param in agent.parameters() if param.requires_grad])

        print(f"Done setting up student! Total params: {num_params}. Trainable Params: {num_trainable_params}")

        params_with_gradients = [name for name, param in model.named_parameters() if param.requires_grad]
        if rank0_only():
            print("Params with gradients")
            pprint(params_with_gradients)

        return agent


    def update_stats(self, episode_stats):
        stats_keys = sorted(episode_stats.keys())
        episode_stats = torch.tensor([episode_stats[key] for key in stats_keys],
                                     device='cpu',
                                     dtype=torch.float32)
        episode_stats = all_reduce(self.is_distributed, self.device, episode_stats)
        episode_stats /= torch.distributed.get_world_size()

        episode_stats = { k: episode_stats[i].item() for i, k in enumerate(stats_keys) }

        self.cumstats['step'] = self.step
        self.cumstats['epoch'] = self.epoch
        self.cumstats['metrics/total_frames'] += int(episode_stats['metrics/frames'] * torch.distributed.get_world_size())

        return {
            **self.cumstats,
            **episode_stats
        }


    def train_bc_step(self, episodes):
        T = self.config.train.policy.max_trajectory_length
        batch_size = self.config.train.batch_size
        minibatch_size = self.config.train.minibatch_size
        num_minibatches = batch_size // minibatch_size
        num_grad_accums = self.config.train.num_grad_accums

        rgbs = [einops.rearrange(episode['rgb'], 't h w c -> t c h w') for episode in episodes]
        goals = [einops.rearrange(episode['imagegoal'], 't h w c -> t c h w') for episode in episodes]
        actions = [episode['action'] for episode in episodes]

        stats = { 'learner/loss': 0.0 }

        rgbs, goals, actions = construct_subsequences(batch_size, T, rgbs, goals, actions)
        batch_idxs = torch.randperm(len(rgbs)).view(num_minibatches, minibatch_size)

        for mb in range(0, num_minibatches, num_grad_accums):

            for g in range(num_grad_accums):
                mb_idxs = batch_idxs[mb + g] 

                # construct batch
                rgbs_t, goals_t, actions_t = map(lambda t: [t[i] for i in mb_idxs], (rgbs, goals, actions))

                # pad inputs to T
                mask_t = torch.stack([torch.cat([torch.ones(t.shape[0]), torch.zeros(T - t.shape[0])]) for t in rgbs_t])
                mask_t = mask_t.bool()
                rgbs_t = torch.stack([F.pad(t, (0,)*7 + (T - t.shape[0],), 'constant', 0) for t in rgbs_t])
                goals_t = torch.stack(goals_t) 
                actions_t = torch.stack([F.pad(t, (0, T - t.shape[0]), 'constant', 0) for t in actions_t])
                rgbs_t, goals_t, actions_t = apply_transforms_inputs(self.vis_processor, rgbs_t, goals_t, actions_t)

                if g < num_grad_accums - 1:
                    with self.agent.no_sync():
                        outputs = self.agent(rgbs_t, goals_t, actions_t, mask_t)
                        loss = outputs.loss
                        stats['learner/loss'] += loss.item()
                        loss.backward()
                else:
                    outputs = self.agent(rgbs_t, goals_t, actions_t, mask_t)
                    loss = outputs.loss
                    stats['learner/loss'] += loss.item()
                    loss.backward()

                rgbs_t.to('cpu')
                goals_t.to('cpu')

            if self.config.train.max_grad_norm is not None:
                torch.nn.utils.clip_grad_norm_(self.agent.parameters(), self.config.train.max_grad_norm)
            self.optim.step()
            self.optim.zero_grad()


        stats['learner/loss'] /= num_minibatches
        stats['learner/lr'] = self.lr_scheduler.get_last_lr()[0]
        stats['metrics/frames'] = sum([episode['rgb'].shape[0] for episode in episodes])

        return stats


    def load_checkpoint(self, ckpt_path):
        # load checkpoint
        print(f"Loading model from checkpoint")
        ckpt_state_dict = torch.load(ckpt_path, map_location='cpu')
        self.agent.load_state_dict(ckpt_state_dict['model'], strict=False)
        self.optim.load_state_dict(ckpt_state_dict['optimizer'])
        self.lr_scheduler.load_state_dict(ckpt_state_dict['lr_scheduler'])
        self.sampler.load_state_dict(ckpt_state_dict['sampler'])        

        # TODO; check if saved and loaded configs are the same

        # update cum stats
        self.cumstats = ckpt_state_dict['stats']
        self.step = self.cumstats['step']
        self.epoch = self.cumstats['epoch']


    def save_checkpoint(self):
        # only save parameters that have been updated
        param_grad_dict = {
            k: v.requires_grad for k, v in self.agent.named_parameters()
        }

        state_dict = self.agent.state_dict()
        for k in list(state_dict.keys()):
            if k in param_grad_dict.keys() and not param_grad_dict[k]:
                del state_dict[k]

        save_obj = {
            "model": state_dict,
            "optimizer": self.optim.state_dict(),
            "lr_scheduler": self.lr_scheduler.state_dict(),
            "sampler": self.sampler.state_dict(),
            "config": OmegaConf.to_container(self.config),
            "stats": self.cumstats
        }

        ckpt_num = self.step // self.config.train.ckpt_freq
        ckpt_filepath = os.path.join(self.exp_folder, 'ckpts', f'ckpt.{ckpt_num}.pth')
        torch.save(save_obj, ckpt_filepath) 

        artifact_name = f'{self.config.exp.group}-{self.config.exp.job_type}-{self.config.exp.name}'
        artifact_name = artifact_name.replace('+', '_')
        artifact_name = artifact_name.replace('=', '_')
        self.writer.save_artifact(artifact_name, 'model', os.path.abspath(ckpt_filepath))

    def train(self):
        self.initialize_train()

        while self.step < self.config.train.steps:

            for batch in self.data_loader:
                stats = self.train_bc_step(batch)

                self.lr_scheduler.step()
                torch.distributed.barrier()
                stats = self.update_stats(stats)

                if rank0_only():
                    self.writer.write(stats)
                    if self.step % self.config.train.ckpt_freq == 0:
                        self.save_checkpoint()

                self.step += 1

            self.epoch += 1


    def save_episode_video(self, episode, num_episodes, video_dir, ckpt_idx):
        obs_infos = [(step['observation'], step['info']) for step in episode]
        _, infos = zip(*obs_infos)

        frames = [observations_to_image(obs, info) for obs, info in obs_infos]
        disp_info = {k: [info[k] for info in infos] for k in infos[0].keys()}

        generate_video(
            video_option=['disk'],
            video_dir=video_dir,
            images=frames,
            episode_id=num_episodes,
            checkpoint_idx=ckpt_idx,
            metrics=extract_scalars_from_info(disp_info),
            fps=self.config.habitat_baselines.video_fps,
            tb_writer=None,
            keys_to_include_in_name=self.config.habitat_baselines.eval_keys_to_include_in_name
        )


    def eval(self):
        self.initialize_eval()

        assert self.config.eval.policy.use_artifact_policy_config, "eval was selected, but no artifact was provided!"

        if self.config.eval.policy.load_artifact.version == '*':
            versions = self.writer.load_model_versions(self.config.eval.policy.load_artifact)
        else:
            versions = [self.config.eval.policy.load_artifact.version]

        versions = reversed(sorted(versions))  
        for version in versions:
            with read_write(self.config):
                self.config.eval.policy.load_artifact.version = version
            ckpt_path = self.writer.load_model(self.config.eval.policy.load_artifact)
            stats_path = os.path.join(self.eval_dir, os.path.basename(ckpt_path), 'stats.pkl')

            if os.path.exists(stats_path):
                with open(stats_path, 'rb') as f:
                    prev_stats = pickle.load(f)
            else:
                prev_stats = None

            self.eval_checkpoint(ckpt_path, prev_stats)


    def embed_observations(self, observations):
        observations = batch_obs(observations, self.device)
        rgbs, goals = map(lambda t: einops.rearrange(t, 'b h w c -> b 1 c h w'), (observations['rgb'], observations['imagegoal']))
        rgbs_t, goals_t = apply_transforms_images(self.vis_processor, rgbs, goals) 
        img_embds_t, img_atts_t = self.agent.embed_visual(torch.cat([rgbs_t, goals_t], dim=2).to(self.device))
        rgb_embds, goal_embds = img_embds_t[:, 0], img_embds_t[:, 1]

        map(lambda t: t.to('cpu'), (observations['rgb'], observations['imagegoal'], observations['depth']))
        del observations
        return rgb_embds, goal_embds


    def eval_checkpoint(self, ckpt_path, prev_stats):
        print(f"Starting evaluation for {ckpt_path}")

        N_episodes = self.config.eval.num_episodes
        T = self.config.train.policy.max_trajectory_length

        # construct directory to save stats
        ckpt_name = os.path.basename(ckpt_path)
        eval_dir = os.path.join(self.eval_dir, ckpt_name)
        video_dir = os.path.join(eval_dir, 'videos')
        os.makedirs(eval_dir, exist_ok=True)

        if self.config.eval.save_videos:
            os.makedirs(video_dir, exist_ok=True)

        # load checkpoint
        print(f"Loading model from checkpoint")
        ckpt_state_dict = torch.load(ckpt_path)
        ckpt_state_dict = { k[len('module.'):]:v for k, v in ckpt_state_dict['model'].items() }
        self.agent.load_state_dict(ckpt_state_dict, strict=False)

        # turn of all gradients
        for param in self.agent.parameters():
            param.requires_grad = False

        observations = self.envs.reset()
        episodes = [[] for _ in range(self.envs.num_envs)]
        dones = [False for _ in range(self.envs.num_envs)]

        stats = {
            f'{ckpt_name}/total_episodes': 0,
            f'{ckpt_name}/successful_episodes': 0,
        }

        if prev_stats is not None:
            stats = prev_stats

        actor = self.agent.action_generator(self.envs.num_envs, deterministic=self.config.eval.deterministic)

        while stats[f'{ckpt_name}/total_episodes'] < N_episodes:
            next(actor)
            actions = actor.send((self.embed_observations(observations), dones)) 

            outputs = self.envs.step(actions)
            next_observations, rewards_l, dones, infos = [list(x) for x in zip(*outputs)] 

            # add environment observation to episodes list
            for i in range(len(episodes)):
                episodes[i].append({
                    'observation': observations[i],
                    'reward': rewards_l[i],
                    'info': infos[i],
                    'action': actions[i]
                })

            for i, done in enumerate(dones):
                if not done:
                    continue
                stats[f'{ckpt_name}/total_episodes'] += 1

                if episodes[i][-1]['info']['distance_to_goal'] < self.config.eval.dtg_threshold:
                    stats[f'{ckpt_name}/successful_episodes'] += 1

                self.writer.write(stats)
                if self.config.eval.save_videos:
                    try:
                        ckpt_idx = ckpt_name.split('.')[1]
                        self.save_episode_video(episodes[i], stats[f'{ckpt_name}/total_episodes'], video_dir, ckpt_idx)
                    except:
                        print("There was an error while saving video!")

                # this is to tell actor generator to clear this episode from history
                episodes[i] = []

            observations = next_observations
        
            with open(os.path.join(eval_dir, 'stats.pkl'), 'wb+') as f:
                pickle.dump(stats, f)
         

def main():
    parser = argparse.ArgumentParser(description="Example argparse for cfg_path")
    parser.add_argument('cfg_path', type=str, help="Path to the configuration file")
    parser.add_argument('--eval', action='store_true', help='Flag to enable evaluation mode')
    parser.add_argument('--debug', action='store_true', help='Flag to enable debug mode')
    parser.add_argument('--resume_run_id', type=str, help="Writer run id to restart")
    args = parser.parse_args()

    config = get_config(args.cfg_path)
    resume_id = args.resume_run_id

    with read_write(config):
        config.exp.resume_id = resume_id

        if args.eval:
            config.habitat_baselines.num_environments = config.eval.num_envs

    trainer = BCTrainer(config, eval=args.eval, verbose=args.debug)

    if not args.eval:
        trainer.train()
    else:
        trainer.eval()


if __name__ == "__main__":
    main()
    

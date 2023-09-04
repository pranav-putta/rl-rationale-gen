from lmnav.data_gen import start_data_gen_process
from lmnav.data_gen import _init_envs, _create_obs_transforms, _setup_teacher
from lmnav.common.episode_processor import extract_inputs_from_dataset, sample_subsequences 

import habitat

import time

import unittest
    
from collections import namedtuple

   
class TestEpisodeProcessor(unittest.TestCase):
    
    def setUp(self):
        self.device = 'cuda:0'
        self.config = habitat.get_config("./lmnav/configs/habitat/imagenav_hm3d.yaml")

    def test_data_gen_process(self):
        process, queue = start_data_gen_process(self.device, self.config, deterministic=False)

        dataset = [queue.get() for _ in range(2)]
        print("Collected 2 episodes!")

        rgbs, goals, actions = extract_inputs_from_dataset(dataset)
        rgbs_t, goals_t, actions_t = sample_subsequences(2, 10, rgbs, goals, actions) 

        print(rgbs_t.shape, goals_t.shape, actions_t.shape)

 
if __name__ == '__main__':
    unittest.main()

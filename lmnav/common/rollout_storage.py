import torch


class RolloutStorage:
    def __init__(self, num_envs, max_steps, tokens_per_img, hidden_size, device):
        self.num_envs = num_envs
        self.max_steps = max_steps

        self.rgbs = torch.zeros(
            num_envs,
            max_steps + 1,
            tokens_per_img,
            hidden_size,
            dtype=torch.bfloat16,
            device=device,
        )
        self.goals = torch.zeros(
            num_envs,
            max_steps + 1,
            tokens_per_img,
            hidden_size,
            dtype=torch.bfloat16,
            device=device,
        )
        self.actions = torch.zeros(
            num_envs, max_steps + 1, dtype=torch.int8, device=device
        )
        self.dones = torch.zeros(
            num_envs, max_steps + 1, dtype=torch.bool, device=device
        )
        self.successes = torch.zeros(
            num_envs, max_steps, dtype=torch.bool, device=device
        )
        self.rewards = torch.zeros(
            num_envs, max_steps + 1, dtype=torch.float, device=device
        )

        self.current_step_idx = -1

    def insert(
        self,
        next_rgbs,
        next_goals,
        dones=None,
        actions=None,
        rewards=None,
        successes=None,
    ):
        """insert observations, dones, and actions into rollout storage tensors"""
        self.rgbs[:, self.current_step_idx + 1] = next_rgbs
        self.goals[:, self.current_step_idx + 1] = next_goals

        if dones is not None:
            self.dones[:, self.current_step_idx] = dones
        if successes is not None:
            self.successes[:, self.current_step_idx] = successes
        if rewards is not None:
            self.rewards[:, self.current_step_idx] = rewards
        if actions is not None:
            self.actions[:, self.current_step_idx] = actions

        self.current_step_idx += 1

    def reset(self):
        self.rgbs[:, 0] = self.rgbs[:, -1]
        self.goals[:, 0] = self.goals[:, -1]

        self.current_step_idx = 0

    def generate_samples(self):
        """
        TODO; is there a better way to do this??

        1. Take episode sequences with history and apply mask or [sep] tokens
        to fill the space when histories are of different length
        2. Keep one continuous episode accumulation with state history max, and
        use [sep] tokens to differentiate between trajectories
         > this doesn't work actually, any past trajectory is screwed up
            becaues the positional embedding needs to be shifted

        Again, this assumes that self.max_steps = model max trajectory

        return rgbs, goals, actions, rewards, dones, successes
        """
        samples = []
        for b in range(self.num_envs):
            ends = torch.where(self.dones[b])[0].tolist()
            # always make sure that ends includes the beginning/end of step list
            ends = [-1] + ends
            if ends[-1] != self.max_steps - 1:
                ends = ends + [self.max_steps - 1]

            slices = [slice(ends[i] + 1, ends[i + 1] + 1) for i in range(len(ends) - 1)]

            samples += [
                tuple(
                    map(
                        lambda t: t[b, s],
                        (
                            self.rgbs,
                            self.goals,
                            self.actions,
                            self.rewards,
                            self.dones,
                            self.successes,
                        ),
                    )
                )
                for s in slices
            ]

        return zip(*samples)

    def to_cpu(self):
        map(
            lambda t: t.cpu(),
            (self.rgbs, self.goals, self.dones, self.rewards, self.actions),
        )

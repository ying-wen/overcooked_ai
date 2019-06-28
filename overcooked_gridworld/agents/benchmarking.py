import os
import json
import tqdm
import numpy as np
from argparse import ArgumentParser

from overcooked_gridworld.utils import load_dict_from_file, get_max_iter, save_pickle, load_pickle, cumulative_rewards_from_rew_list
from overcooked_gridworld.planning.planners import NO_COUNTERS_PARAMS, MediumLevelPlanner, NO_COUNTERS_START_OR_PARAMS
from overcooked_gridworld.mdp.layout_generator import LayoutGenerator
from overcooked_gridworld.agents.agent import AgentPair, CoupledPlanningAgent, RandomAgent, GreedyHumanModel
from overcooked_gridworld.mdp.overcooked_mdp import OvercookedGridworld, Action, NO_REW_SHAPING_PARAMS
from overcooked_gridworld.mdp.overcooked_env import OvercookedEnv


class AgentEvaluator(object):
    """
    Class used to get trajectory rollouts of agents trained with a variety of methods
    """

    def __init__(self, layout_name, order_goal=['any'] * 100, explosion_time=500, start_state=None, horizon=400, force_compute=False, debug=False):
        self.layout_name = layout_name
        self.order_goal = order_goal
        self.explosion_time = explosion_time
        self.start_state = start_state
        self.horizon = horizon
        self.force_compute = force_compute
        self.debug = debug
        self._mlp = None
        self._mdp = None
        self._env = None

    @staticmethod
    def from_config(env_config, start_state=None):
        ae = AgentEvaluator(
            layout_name=env_config["FIXED_MDP"], 
            order_goal=env_config["ORDER_GOAL"],
            explosion_time=env_config["EXPLOSION_TIME"],
            start_state=start_state,
            horizon=env_config["ENV_HORIZON"]
        )
        from hr_coordination.pbt.pbt_utils import setup_mdp_env
        ae._env = setup_mdp_env(display=False, **env_config)
        ae._mdp = ae._env.mdp
        ae.config = env_config
        return ae

    @staticmethod
    def from_pbt_dir(run_dir, start_state=None):
        from hr_coordination.pbt.pbt_utils import setup_mdp_env, get_config_from_pbt_dir
        config = get_config_from_pbt_dir(run_dir)
        return AgentEvaluator.from_config(config, start_state)

    @property
    def mdp(self):
        if self._mdp is None:
            if self.debug: print("Computing Mdp")
            self._mdp = OvercookedGridworld.from_file(self.layout_name, self.order_goal, self.explosion_time, rew_shaping_params=None)
        return self._mdp

    @property
    def env(self):
        if self._env is None:
            if self.debug: print("Computing Env")
            self._env = OvercookedEnv(self.mdp, start_state=self.start_state, horizon=self.horizon, random_start_objs=False, random_start_pos=False)
        return self._env

    @property
    def mlp(self):
        if self._mlp is None:
            if self.debug: print("Computing Planner")
            self._mlp = MediumLevelPlanner.from_pickle_or_compute(self.layout_name + "_am.pkl", self.mdp, NO_COUNTERS_PARAMS, force_compute=self.force_compute)
        return self._mlp

    def evaluate_human_model_pair(self, display=True):
        a0 = GreedyHumanModel(self.mlp)
        a1 = GreedyHumanModel(self.mlp)
        agent_pair = AgentPair(a0, a1)
        return self.evaluate_agent_pair(agent_pair, display=display)

    def evaluate_optimal_pair(self, display=True, delivery_horizon=2):
        a0 = CoupledPlanningAgent(self.mlp, delivery_horizon=delivery_horizon)
        a1 = CoupledPlanningAgent(self.mlp, delivery_horizon=delivery_horizon)
        a0.mlp.env = self.env
        a1.mlp.env = self.env
        agent_pair = AgentPair(a0, a1)
        return self.evaluate_agent_pair(agent_pair, display=display)

    def evaluate_one_optimal_one_random(self, display=True):
        a0 = CoupledPlanningAgent(self.mlp)
        a1 = RandomAgent()
        agent_pair = AgentPair(a0, a1)
        return self.evaluate_agent_pair(agent_pair, display=display)

    def evaluate_one_optimal_one_greedy_human(self, h_idx=0, display=True):
        h, r = GreedyHumanModel, CoupledPlanningAgent
        if h_idx == 0:
            a0, a1 = h(self.mlp), r(self.mlp)
        elif h_idx == 1:
            a0, a1 = r(self.mlp), h(self.mlp)
        agent_pair = AgentPair(a0, a1)
        return self.evaluate_agent_pair(agent_pair, display=display)

    def evaluate_agent_pair(self, agent_pair, num_games=1, display=False):
        agent_pair.set_mdp(self.mdp)
        return self.env.get_rollouts(agent_pair, num_games, display=display)

    def get_pbt_agents_trajectories(self, agent0_idx, agent1_idx, num_trajectories, display=False):
        # TODO: Remove this from here and put in PBT utils
        from hr_coordination.pbt.pbt_utils import setup_mdp_env, get_pbt_agent_from_config
        assert self.config, "Class instance has to be initialized with from_pbt_dir"

        agent0 = get_pbt_agent_from_config(self.config, agent0_idx)
        agent1 = get_pbt_agent_from_config(self.config, agent1_idx)

        mdp_env = setup_mdp_env(display=False, **self.config)
        return mdp_env.get_rollouts(AgentPair(agent0, agent1), num_trajectories, display=display, processed=True, final_state=False)

    @staticmethod
    def cumulative_rewards_from_trajectory(trajectory):
        cumulative_rew = 0
        for trajectory_item in trajectory:
            r_t = trajectory_item[2]
            cumulative_rew += r_t
        return cumulative_rew

    def check_trajectories(self, trajectories):
        """Checks consistency of trajectories in standard format with dynamics of mdp."""
        for i in range(len(trajectories["ep_observations"])):
            self.check_trajectory(trajectories, i)

    def check_trajectory(self, trajectories, idx):
        states, actions, rewards = trajectories["ep_observations"][idx], trajectories["ep_actions"][idx], trajectories["ep_rewards"][idx]

        assert len(states) == len(actions)
        # TODO: check dones positions, lengths consistency

        # Checking that actions would give rise to same behaviour in current MDP
        simulation_env = self.env.copy()
        for i in range(len(states)):
            curr_state = states[i]
            curr_state.order_list = ["onion"] * 3 # NOTE: hack that should be fixed upstream in mdp code
            simulation_env.state = curr_state

            if i + 1 < len(states):
                next_state, reward, done, info = simulation_env.step(actions[i])
                next_state.order_list = ["onion"] * 3 # NOTE: same as above

                assert states[i + 1] == next_state, "States differed (expected vs actual): {}".format(
                    simulation_env.display_states(states[i + 1], next_state)
                )
                assert rewards[i] == reward, "{} \t {}".format(rewards[i], reward)
            

    ### I/O METHODS ###

    def save_trajectory(self, trajectory, filename):
        trajectory_dict_standard_signature = [
            "ep_actions", "ep_observations", "ep_rewards", "ep_dones", "ep_returns", "ep_lengths"
        ]
        assert set(trajectory.keys()) == set(trajectory_dict_standard_signature)
        self.check_trajectories(trajectory)
        save_pickle(trajectory, filename)

    def load_trajectory(self, filename):
        traj = load_pickle(filename)
        self.check_trajectories(traj)
        return traj

    @staticmethod
    def save_traj_in_baselines_format(rollout_trajs, filename):
        """Useful for GAIL and behavioral cloning"""
        np.savez(
            filename,
            obs=rollout_trajs["ep_observations"],
            acs=rollout_trajs["ep_actions"],
            ep_lens=rollout_trajs["ep_lengths"],
            ep_rets=rollout_trajs["ep_returns"],
        )
    
    @staticmethod
    def save_traj_in_stable_baselines_format(rollout_trajs, filename):
        # Converting episode dones to episode starts
        eps_starts = [np.zeros(len(traj)) for traj in rollout_trajs["ep_dones"]]
        for ep_starts in eps_starts:
            ep_starts[0] = 1
        eps_starts = [ep_starts.astype(np.bool) for ep_starts in eps_starts]

        stable_baselines_trajs_dict = {
            'actions': np.concatenate(rollout_trajs["ep_actions"]),
            'obs': np.concatenate(rollout_trajs["ep_observations"]),
            'rewards': np.concatenate(rollout_trajs["ep_rewards"]),
            'episode_starts': np.concatenate(eps_starts),
            'episode_returns': rollout_trajs["ep_returns"]
        }
        stable_baselines_trajs_dict = { k:np.array(v) for k, v in stable_baselines_trajs_dict.items() }
        np.savez(filename, **stable_baselines_trajs_dict)

    def save_action_traj_for_viz(self, trajectory, path):
        """
        Trajectory will be a list of state-action pairs (s_t, a_t, r_t).
        NOTE: Used mainly to visualize trajectories in overcooked-js repo.
        """
        # Add trajectory to json
        traj = []

        # NOTE: Assumes only one trajectory
        for a_t in trajectory['ep_actions'][0]:
            a_modified = [a if a != Action.INTERACT else a.upper() for a in a_t]
            if all([a is not None for a in a_t]):
                traj.append(a_modified)

        json_traj = {}
        json_traj["traj"] = traj

        # Add layout grid to json
        mdp_grid = []
        for row in self.mdp.terrain_mtx:
            mdp_grid.append("".join(row))

        for i, start_pos in enumerate(self.mdp.start_player_positions):
            x, y = start_pos
            row_string = mdp_grid[y]
            new_row_string = row_string[:x] + str(i + 1) + row_string[x+1:]
            mdp_grid[y] = new_row_string

        json_traj["mdp_grid"] = mdp_grid

        with open(path + '.json', 'w') as filename:  
            json.dump(json_traj, filename)

    # Clean this if unnecessary
    # trajectory, time_taken = self.env.run_agents(agent_pair, display=display)
    # tot_rewards = self.cumulative_rewards_from_trajectory(trajectory)
    # return tot_rewards, time_taken, trajectory

    @staticmethod
    def interactive_from_traj(trajs, traj_idx):
        """Displays ith trajectory of trajs interactively in a Jupyter notebook"""
        from ipywidgets import widgets, interactive_output

        states = trajs["ep_observations"][traj_idx]
        joint_actions = trajs["ep_actions"][traj_idx]
        cumulative_rewards = cumulative_rewards_from_rew_list(trajs["ep_rewards"][traj_idx])
        layout_name = trajs["layout_name"]
        env = AgentEvaluator(layout_name).env

        def update(t = 1.0):
            env.state = states[int(t)]
            print(env)
            joint_action = joint_actions[int(t)]
            print("Joint Action: {} \t Score: {}".format(Action.joint_action_to_char(joint_action), cumulative_rewards[t]))
            
        t = widgets.IntSlider(min=0, max=len(states) - 1, step=1, value=0)
        out = interactive_output(update, {'t': t})
        display(out, t)





# SAMPLE SCRIPTS    

# Getting Trajs From Optimal Planner
# eva = AgentEvaluator("scenario2")
# tot_rewards, time_taken, trajectory = eva.evaluate_optimal_pair(["any"] * 3)
# eva.dump_trajectory_as_json(trajectory, "../overcooked-js/simple_rr")
# print("done")

# Getting Trajs from pbt Agent
# eva = AgentEvaluator.from_pbt_dir(run_dir="2019_03_20-10_53_03_scenario2_no_rnd_objs", seed_idx=0)
# ep_rews, ep_lens, ep_obs, ep_acts = eva.get_pbt_agents_trajectories(agent0_idx=0, agent1_idx=0, num_trajectories=1)
# eva.dump_trajectory_as_json(trajectory, "data/agent_runs/")


# if __name__ == "__main__" :
#     parser = ArgumentParser()
#     parser.add_argument("-t", "--type", dest="type",
#                         help="type of run: ['rollouts', 'ppo']", required=True)
#     parser.add_argument("-r", "--run_name", dest="run",
#                         help="name of run in data/*_runs/", required=True)
#     parser.add_argument("-a", "--agent_num", dest="agent_num", default=0)
#     parser.add_argument("-i", "--idx", dest="idx", default=0)

#     args = parser.parse_args()

#     run_type, run_name, player_idx, agent_num = args.type, args.run, int(args.idx), int(args.agent_num)
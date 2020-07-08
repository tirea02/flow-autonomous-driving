import argparse
import json
import os
import sys
from time import strftime
from copy import deepcopy
import numpy as np
import torch as th

from flow.core.util import ensure_dir
from flow.utils.registry import env_constructor
from flow.utils.rllib import FlowParamsEncoder, get_flow_params
from flow.utils.registry import make_create_env
from flow.core.experiment import Experiment


def parse_args(args):
    """Parse training options user can specify in command line.
    Returns
    -------
    argparse.Namespace
        the output parser object
    """
    # parser = argparse.ArgumentParser(
    #     formatter_class=argparse.RawDescriptionHelpFormatter,
    #     description="Parse argument used when running a Flow simulation.",
    #     epilog="python simulate.py EXP_CONFIG")
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Parse argument used when running a Flow simulation.",
        epilog="python simulate.py EXP_CONFIG")
    # required input parameters
    parser.add_argument(
        'exp_config', type=str,
    )  # Name of the experiment configuration file, as located in
    # exp_configs/non_rl exp_configs/rl/singleagent or exp_configs/rl/multiagent.'

    # optional input parameters (for RL parser)
    parser.add_argument(
        '--rl_trainer', type=str, default="stable-baselines",
    )  # the RL trainer to use. either  or Stable-Baselines
    parser.add_argument(
        '--num_cpus', type=int, default=1,
    )  # How many CPUs to use
    parser.add_argument(
        '--num_steps', type=int, default=5000,
    )  # How many total steps to perform learning over
    parser.add_argument(
        '--rollout_size', type=int, default=1000,
    )  # How many steps are in a training batch.
    parser.add_argument(
        '--checkpoint_path', type=str, default=None,
    )  # Directory with checkpoint to restore training from.

    # for non-RL parser
    parser.add_argument(
        '--gen_emission',
        action='store_true',
    )  # Specifies whether to generate an emission file from the simulation.
    parser.add_argument(
        '--num_runs', type=int, default=1,
    )  # Number of simulations to run. Defaults to 1.
    parser.add_argument(
        '--no_render',
        action='store_true',
    )  # Specifies whether to run the simulation during runtime.
    return parser.parse_known_args(args)[0]

# stable-baseline


def run_model_stablebaseline(flow_params,
                             num_cpus=1,
                             rollout_size=50,
                             num_steps=50):

    from stable_baselines.common.vec_env import DummyVecEnv, SubprocVecEnv
    from stable_baselines import DDPG
    from stable_baselines.deepq.policies import MlpPolicy
    from stable_baselines.common.noise import NormalActionNoise, OrnsteinUhlenbeckActionNoise, AdaptiveParamNoiseSpec
    if num_cpus == 1:
        constructor = env_constructor(params=flow_params, version=0)()
        # The algorithms require a vectorized environment to run
        env = DummyVecEnv([lambda: constructor])
    else:
        env = SubprocVecEnv([env_constructor(params=flow_params, version=i)
                             for i in range(num_cpus)])

    n_actions = env.action_space.shape[-1]
    param_noise = None
    action_noise = OrnsteinUhlenbeckActionNoise(
        mean=np.zeros(n_actions), sigma=float(0.5) * np.ones(n_actions))
    train_model = DDPG('MlpPolicy', env, verbose=1,
                       param_noise=param_noise, action_noise=action_noise)
    train_model.learn(total_timesteps=num_steps)
    return train_model


def train_stable_baselines(submodule, flags):
    """Train policies using the PPO algorithm in stable-baselines."""
    from stable_baselines.common.vec_env import DummyVecEnv
    from stable_baselines import DDPG
    flow_params = submodule.flow_params
    # Path to the saved files
    exp_tag = flow_params['exp_tag']
    result_name = '{}/{}'.format(exp_tag, strftime("%Y-%m-%d-%H:%M:%S"))

    # Perform training.
    print('Beginning training.')
    model = run_model_stablebaseline(
        flow_params, flags.num_cpus, flags.rollout_size, flags.num_steps)

    # Save the model to a desired folder and then delete it to demonstrate
    # loading.
    print('Saving the trained model!')
    path = os.path.realpath(os.path.expanduser('~/baseline_results'))
    ensure_dir(path)
    save_path = os.path.join(path, result_name)
    model.save(save_path)
    print("check")
    # dump the flow params
    with open(os.path.join(path, result_name) + '.json', 'w') as outfile:
        json.dump(flow_params, outfile,
                  cls=FlowParamsEncoder, sort_keys=True, indent=4)

    # Replay the result by loading the model
    print('Loading the trained model and testing it out!')
    model = DDPG.load(save_path)
    flow_params = get_flow_params(os.path.join(path, result_name) + '.json')
    flow_params['sim'].render = True
    env = env_constructor(params=flow_params, version=0)()

    # The algorithms require a vectorized environment to run
    eval_env = DummyVecEnv([lambda: env])
    obs = eval_env.reset()
    reward = 0
    for _ in range(flow_params['env'].horizon):
        action, _states = model.predict(obs)
        obs, rewards, dones, info = eval_env.step(action)
        reward += rewards
    print('the final reward is {}'.format(reward))

# rllib


def setup_exps_rllib(flow_params,
                     n_cpus,
                     n_rollouts,
                     policy_graphs=None,
                     policy_mapping_fn=None,
                     policies_to_train=None):
    from ray import tune
    from ray.tune.registry import register_env
    try:
        from ray.rllib.agents.agent import get_agent_class
    except ImportError:
        from ray.rllib.agents.registry import get_agent_class

    horizon = flow_params['env'].horizon

    alg_run = "PPO"

    agent_cls = get_agent_class(alg_run)
    config = deepcopy(agent_cls._default_config)

    config["num_workers"] = n_cpus
    config["train_batch_size"] = horizon * n_rollouts
    config["gamma"] = 0.999  # discount rate
    config["model"].update({"fcnet_hiddens": [32, 32, 32]})
    config["use_gae"] = True
    config["lambda"] = 0.97
    config["kl_target"] = 0.02
    config["num_sgd_iter"] = 10
    config["horizon"] = horizon

    # save the flow params for replay
    flow_json = json.dumps(
        flow_params, cls=FlowParamsEncoder, sort_keys=True, indent=4)
    config['env_config']['flow_params'] = flow_json
    config['env_config']['run'] = alg_run

    # multiagent configuration
    if policy_graphs is not None:
        print("policy_graphs", policy_graphs)
        config['multiagent'].update({'policies': policy_graphs})
    if policy_mapping_fn is not None:
        config['multiagent'].update(
            {'policy_mapping_fn': tune.function(policy_mapping_fn)})
    if policies_to_train is not None:
        config['multiagent'].update({'policies_to_train': policies_to_train})

    create_env, gym_name = make_create_env(params=flow_params)

    # Register as rllib env
    register_env(gym_name, create_env)
    return alg_run, gym_name, config


def train_rllib(submodule, flags):
    """Train policies using the PPO algorithm in RLlib."""
    import ray
    from ray.tune import run_experiments

    flow_params = submodule.flow_params
    n_cpus = submodule.N_CPUS
    n_rollouts = submodule.N_ROLLOUTS
    policy_graphs = getattr(submodule, "POLICY_GRAPHS", None)
    policy_mapping_fn = getattr(submodule, "policy_mapping_fn", None)
    policies_to_train = getattr(submodule, "policies_to_train", None)

    alg_run, gym_name, config = setup_exps_rllib(
        flow_params, n_cpus, n_rollouts,
        policy_graphs, policy_mapping_fn, policies_to_train)

    ray.init(num_cpus=n_cpus + 1, object_store_memory=200 * 1024 * 1024)
    exp_config = {
        "run": alg_run,
        "env": gym_name,
        "config": {
            **config
        },
        "checkpoint_freq": 20,
        "checkpoint_at_end": True,
        "max_failures": 999,
        "stop": {
            "training_iteration": flags.num_steps,
        },
    }

    if flags.checkpoint_path is not None:
        exp_config['restore'] = flags.checkpoint_path
    run_experiments({flow_params["exp_tag"]: exp_config})

# simulate without rl


def simulate_without_rl(flags, module):
    flow_params = getattr(module, flags.exp_config).flow_params

    if hasattr(getattr(module, flags.exp_config), "custom_callables"):
        callables = getattr(module, flags.exp_config).custom_callables
    else:
        callables = None
    flow_params['sim'].render = not flags.no_render
    flow_params['simulator'] = 'traci'

    # Specify an emission path if they are meant to be generated.
    if flags.gen_emission:
        flow_params['sim'].emission_path = "./data"

        # Create the flow_params object
        fp_ = flow_params['exp_tag']
        dir_ = flow_params['sim'].emission_path
        with open(os.path.join(dir_, "{}.json".format(fp_)), 'w') as outfile:
            json.dump(flow_params, outfile,
                      cls=FlowParamsEncoder, sort_keys=True, indent=4)
    flow_params['env'].horizon = 1500
    # Create the experiment object.
    exp = Experiment(flow_params, callables)

    # Run for the specified number of rollouts.
    exp.run(flags.num_runs, convert_to_csv=flags.gen_emission)

# stablebaseline_ddpg


def run_model_stablebaseline3(flow_params,
                              num_cpus=1,
                              rollout_size=5,
                              num_steps=5):
    from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv
    from stable_baselines3 import PPO
    from stable_baselines3.ppo import MlpPolicy
    if num_cpus == 1:
        constructor = env_constructor(params=flow_params, version=0)()
        # The algorithms require a vectorized environment to run
        env = DummyVecEnv([lambda: constructor])
    else:
        env = SubprocVecEnv([env_constructor(params=flow_params, version=i)
                             for i in range(num_cpus)])

    train_model = PPO(MlpPolicy, env=env, verbose=1)
    train_model.learn(total_timesteps=num_steps)
    return train_model


def train_stable_baselines3(submodule, flags):
    """Train policies using the PPO algorithm in stable-baselines3."""
    from stable_baselines3.common.vec_env import DummyVecEnv
    from stable_baselines3 import PPO
    flow_params = submodule.flow_params
    # Path to the saved files
    exp_tag = flow_params['exp_tag']
    result_name = '{}/{}'.format(exp_tag, strftime("%Y-%m-%d-%H:%M:%S"))

    # Perform training.
    print('Beginning training.')
    model = run_model_stablebaseline(
        flow_params, flags.num_cpus, flags.rollout_size, flags.num_steps)

    # Save the model to a desired folder and then delete it to demonstrate
    # loading.
    print('Saving the trained model!')
    path = os.path.realpath(os.path.expanduser('~/baseline_results'))
    ensure_dir(path)
    save_path = os.path.join(path, result_name)
    model.save(save_path)
    # dump the flow params

    with open(os.path.join(path, result_name) + '.json', 'w') as outfile:
        json.dump(flow_params, outfile,
                  cls=FlowParamsEncoder, sort_keys=True, indent=4)

    # Replay the result by loading the model
    print('Loading the trained model and testing it out!')
    model.load(save_path)
    flow_params = get_flow_params(os.path.join(path, result_name) + '.json')
    flow_params['sim'].render = True
    env = env_constructor(params=flow_params, version=0)()

    # The algorithms require a vectorized environment to run
    eval_env = DummyVecEnv([lambda: env])
    obs = eval_env.reset()
    reward = 0
    for _ in range(flow_params['env'].horizon):
        action, _states = model.predict(obs)
        obs, rewards, dones, info = eval_env.step(action)
        reward += rewards
    print('the final reward is {}'.format(reward))


def main(args):
    """Perform the training operations."""
    # Parse script-level arguments (not including package arguments).
    flags = parse_args(args)

    # Import relevant information from the exp_config script.
    module = __import__(
        "exp_configs.rl.singleagent", fromlist=[flags.exp_config])
    module_ma = __import__(
        "exp_configs.rl.multiagent", fromlist=[flags.exp_config])
    module_nonrl = __import__(
        "exp_configs.non_rl", fromlist=[flags.exp_config])

    # Import the sub-module containing the specified exp_config and determine
    # whether the environment is single agent or multi-agent.
    # non_rl part
    if hasattr(module_nonrl, flags.exp_config):

        simulate_without_rl(flags, module_nonrl)
        return
    # rl part
    if hasattr(module, flags.exp_config):
        submodule = getattr(module, flags.exp_config)
        multiagent = False
    elif hasattr(module_ma, flags.exp_config):
        submodule = getattr(module_ma, flags.exp_config)
        assert flags.rl_trainer.lower() in ["rllib", "h-baselines"], \
            "Currently, multiagent experiments are only supported through "\
            "RLlib. Try running this experiment using RLlib: " \
            "'python train.py EXP_CONFIG'"
        multiagent = True
    else:
        raise ValueError("Unable to find experiment config.")

    # Perform the training operation.
    if flags.rl_trainer.lower() == "stable-baselines":
        train_stable_baselines(submodule, flags)
    elif flags.rl_trainer.lower() == "rllib":
        train_rllib(submodule, flags)
    elif flags.rl_trainer.lower() == "stable-baselines3":
        train_stable_baselines3(submodule, flags)
    else:
        raise ValueError("rl_trainer should be either 'rllib', 'stable-baselines3', "
                         "or 'stable-baselines'.")


if __name__ == "__main__":
    main(sys.argv[1:])

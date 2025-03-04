# Copyright 2022 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Run deep online mirror descent algorithm with Munchausen DQN agents."""

import os
import pickle
from typing import Sequence

from absl import flags
from absl import logging
import jax
from jax.config import config

from open_spiel.python.utils import gfile
from open_spiel.python import policy
from open_spiel.python import rl_environment
from open_spiel.python.games import dynamic_routing_data
from open_spiel.python.mfg.algorithms import distribution
from open_spiel.python.mfg.algorithms import munchausen_deep_mirror_descent
from open_spiel.python.mfg.algorithms import nash_conv
from open_spiel.python.mfg.algorithms import policy_value
from open_spiel.python.mfg.games import crowd_modelling  # pylint: disable=unused-import
from open_spiel.python.mfg.games import dynamic_routing
from open_spiel.python.mfg.games import predator_prey  # pylint: disable=unused-import
import pyspiel
from open_spiel.python.utils import app

# pylint: disable=g-import-not-at-top
try:
  from clu import metric_writers
except ImportError as e:
  raise ImportError(
      str(e) +
      "\nCLU not found. Please install CLU: python3 -m pip install clu") from e
# pylint: enable=g-import-not-at-top

FLAGS = flags.FLAGS

flags.DEFINE_string("game_name", "mfg_crowd_modelling_2d", "Name of the game.")
flags.DEFINE_string(
    "env_setting", "mfg_crowd_modelling_2d_four_rooms",
    "Name of the game settings. If None, the game name will be used.")

# Training options.
flags.DEFINE_integer("batch_size", 128,
                     "Number of transitions to sample at each learning step.")
flags.DEFINE_integer("learn_every", 64,
                     "Number of steps between learning updates.")
flags.DEFINE_integer("num_episodes_per_iteration", 1000,
                     "Number of training eepisodes for each iteration.")
flags.DEFINE_integer("num_iterations", 100, "Number of iterations.")
flags.DEFINE_integer("epsilon_decay_duration", 100000,
                     "Number of game steps over which epsilon is decayed.")
flags.DEFINE_float("epsilon_power", 1, "Power for the epsilon decay.")
flags.DEFINE_float("epsilon_start", 0.1, "Starting exploration parameter.")
flags.DEFINE_float("epsilon_end", 0.1, "Final exploration parameter.")
flags.DEFINE_float("discount_factor", 1.0,
                   "Discount factor for future rewards.")
flags.DEFINE_bool(
    "reset_replay_buffer_on_update", False,
    "Reset the replay buffer when the softmax policy is updated.")
flags.DEFINE_integer("seed", 42, "Training seed.")
# Evaluation options.
flags.DEFINE_integer("eval_every", 200,
                     "Episode frequency at which the agents are evaluated.")
# Network options.
flags.DEFINE_list("hidden_layers_sizes", ["128", "128"],
                  "Number of hidden units in the avg-net and Q-net.")
flags.DEFINE_integer("update_target_network_every", 200,
                     "Number of steps between DQN target network updates.")
# Replay buffer options.
flags.DEFINE_integer("replay_buffer_capacity", 40000,
                     "Size of the replay buffer.")
flags.DEFINE_integer("min_buffer_size_to_learn", 1000,
                     "Number of samples in buffer before learning begins.")
# Loss and optimizer options.
flags.DEFINE_enum("optimizer", "adam", ["sgd", "adam"], "Optimizer.")
flags.DEFINE_float("learning_rate", 0.01, "Learning rate for inner rl agent.")
flags.DEFINE_enum("loss", "mse", ["mse", "huber"], "Loss function.")
flags.DEFINE_float("huber_loss_parameter", 1.0, "Parameter for Huber loss.")
flags.DEFINE_float("gradient_clipping", None, "Value to clip the gradient to.")
# Munchausen options.
flags.DEFINE_float("tau", 10, "Temperature parameter in Munchausen target.")
flags.DEFINE_float("alpha", 0.99, "Alpha parameter in Munchausen target.")
flags.DEFINE_bool("with_munchausen", True,
                  "If true, target uses Munchausen penalty terms.")
# Logging options.
flags.DEFINE_bool("use_checkpoints", False, "Save/load neural network weights.")
flags.DEFINE_string("checkpoint_dir", "/tmp/dqn_test",
                    "Directory to save/load the agent.")
flags.DEFINE_string(
    "logdir", None,
    "Logging dir to use for TF summary files. If None, the metrics will only "
    "be logged to stderr.")
flags.DEFINE_bool("log_distribution", False,
                  "Enables logging of the distribution.")

GAME_SETTINGS = {
    # 4 rooms exploration example for crowd modeling 2D
    "mfg_crowd_modelling_2d_four_rooms": {
        "size": 13,
        "horizon": 40,
        "only_distribution_reward": True,
        "forbidden_states":
            "[1|6;2|6;4|6;5|6;6|1;6|2;6|4;6|5;6|6;6|7;6|8;6|10;6|11;7|6;8|6;10"
            "|6;11|6;0|0;0|1;0|2;0|3;0|4;0|5;0|6;0|7;0|8;0|9;0|10;0|11;0|12;12"
            "|0;12|1;12|2;12|3;12|4;12|5;12|6;12|7;12|8;12|9;12|10;12|11;12"
            "|12;0|0;1|0;2|0;3|0;4|0;5|0;6|0;7|0;8|0;9|0;10|0;11|0;12|0;0|12;1"
            "|12;2|12;3|12;4|12;5|12;6|12;7|12;8|12;9|12;10|12;11|12;12|12]",
        "initial_distribution": "[1|1]",
        "initial_distribution_value": "[1.0]",
    },
    "dynamic_routing_braess": {
        "max_num_time_step": 100,
        "network": "braess",
        "time_step_length": 0.05,
    },
}

DYNAMIC_ROUTING_NETWORK = {
    "line": (dynamic_routing_data.LINE_NETWORK,
             dynamic_routing_data.LINE_NETWORK_OD_DEMAND),
    "braess": (dynamic_routing_data.BRAESS_NETWORK,
               dynamic_routing_data.BRAESS_NETWORK_OD_DEMAND),
}


def save_distribution(filename: str, dist: distribution.DistributionPolicy):
  """Saves the distribution to a file."""
  with gfile.Open(filename, "wb") as f:
    # This will be a mapping from the string representation of the states to
    # the probabilities.
    pickle.dump(dist.distribution, f, protocol=pickle.HIGHEST_PROTOCOL)


def main(argv: Sequence[str]) -> None:
  if len(argv) > 1:
    raise app.UsageError("Too many command-line arguments.")

  game_name = FLAGS.game_name
  logging.info("Loading %s", game_name)
  settings = GAME_SETTINGS.get(FLAGS.env_setting or game_name, {})
  logging.info("Using settings %r", settings)
  if game_name == "python_mfg_dynamic_routing":
    network = settings.pop("network")
    network, od_demand = DYNAMIC_ROUTING_NETWORK[network]
    game = dynamic_routing.MeanFieldRoutingGame(
        settings, network=network, od_demand=od_demand)
  else:
    game = pyspiel.load_game(FLAGS.game_name, settings)

  num_players = game.num_players()

  # Create the environments with uniform initial policy.
  uniform_policy = policy.UniformRandomPolicy(game)
  uniform_dist = distribution.DistributionPolicy(game, uniform_policy)

  envs = [
      rl_environment.Environment(  # pylint: disable=g-complex-comprehension
          game,
          mfg_distribution=uniform_dist,
          mfg_population=p,
          observation_type=rl_environment.ObservationType.OBSERVATION)
      for p in range(num_players)
  ]

  env = envs[0]
  info_state_size = env.observation_spec()["info_state"][0]
  num_actions = env.action_spec()["num_actions"]

  # Create the agents.
  kwargs = {
      "alpha": FLAGS.alpha,
      "batch_size": FLAGS.batch_size,
      "discount_factor": FLAGS.discount_factor,
      "epsilon_decay_duration": FLAGS.epsilon_decay_duration,
      "epsilon_end": FLAGS.epsilon_end,
      "epsilon_power": FLAGS.epsilon_power,
      "epsilon_start": FLAGS.epsilon_start,
      "gradient_clipping": FLAGS.gradient_clipping,
      "hidden_layers_sizes": [int(l) for l in FLAGS.hidden_layers_sizes],
      "huber_loss_parameter": FLAGS.huber_loss_parameter,
      "learn_every": FLAGS.learn_every,
      "learning_rate": FLAGS.learning_rate,
      "loss": FLAGS.loss,
      "min_buffer_size_to_learn": FLAGS.min_buffer_size_to_learn,
      "optimizer": FLAGS.optimizer,
      "replay_buffer_capacity": FLAGS.replay_buffer_capacity,
      "reset_replay_buffer_on_update": FLAGS.reset_replay_buffer_on_update,
      "seed": FLAGS.seed,
      "tau": FLAGS.tau,
      "update_target_network_every": FLAGS.update_target_network_every,
      "with_munchausen": FLAGS.with_munchausen
  }
  agents = [
      munchausen_deep_mirror_descent.MunchausenDQN(p, info_state_size,
                                                   num_actions, **kwargs)
      for p in range(num_players)
  ]

  # Metrics writer will also log the metrics to stderr.
  just_logging = FLAGS.logdir is None or jax.host_id() > 0
  writer = metric_writers.create_default_writer(
      FLAGS.logdir, just_logging=just_logging)

  # # Save the parameters.
  writer.write_hparams(kwargs)

  def logging_fn(it, episode, vals):
    writer.write_scalars(it * num_episodes_per_iteration + episode, vals)

  num_episodes_per_iteration = FLAGS.num_episodes_per_iteration
  md = munchausen_deep_mirror_descent.DeepOnlineMirrorDescent(
      game,
      envs,
      agents,
      eval_every=FLAGS.eval_every,
      num_episodes_per_iteration=num_episodes_per_iteration,
      logging_fn=logging_fn)

  def log_metrics(it):
    """Logs the training metrics for each iteration."""
    initial_states = game.new_initial_states()
    pi_value = policy_value.PolicyValue(game, md.distribution, md.policy)
    metrics = {
        f"best_response/{state}": pi_value.eval_state(state)
        for state in initial_states
    }
    nash_conv_md = nash_conv.NashConv(game, md.policy).nash_conv()
    metrics["nash_conv_md"] = nash_conv_md
    if FLAGS.log_distribution and FLAGS.logdir:
      # We log distribution directly to a Pickle file as it may be large for
      # logging as a metric.
      filename = os.path.join(FLAGS.logdir, f"distribution_{it}.pkl")
      save_distribution(filename, md.distribution)
    logging_fn(it, 0, metrics)

  log_metrics(0)
  for it in range(1, FLAGS.num_iterations + 1):
    md.iteration()
    log_metrics(it)

  # Make sure all values were written.
  writer.flush()


if __name__ == "__main__":
  config.parse_flags_with_absl()
  app.run(main)

import os
import numpy
import yaml
import yamlloader
import rclpy
from rclpy.node import Node
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from core.service_client import ServiceClient
from core_interfaces.srv import LoadConfig
from core.utils import class_from_classname

class FruitShopSim(Node):
    """
    FruitShopSim simulator class.
    """
    def __init__(self):
        """
        Constructor of the FruitShopSim simulator class.
        Initializes the simulator with parameters, publishers, and perception messages.
        """
        super().__init__("FruitShopSim")
        self.rng = None
        self.ident = None
        self.base_messages = {}
        self.perceptions = {}
        self.sim_publishers = {}

        self.random_seed = self.declare_parameter('random_seed', value = 0).get_parameter_value().integer_value
        self.config_file = self.declare_parameter('config_file', descriptor=ParameterDescriptor(dynamic_typing=True)).get_parameter_value().string_value
        self.fruits = []
        self.catched_fruit = None
        self.tested_fruit = None

        self.normal_inner = numpy.poly1d(
        numpy.polyfit([0.0, 0.3925, 0.785, 1.1775, 1.57], [0.45, 0.47, 0.525, 0.65, 0.9], 3)
        )
        self.normal_outer = numpy.poly1d(
        numpy.polyfit([0.0, 0.3925, 0.785, 1.1775, 1.57], [1.15, 1.25, 1.325, 1.375, 1.375], 3)
        )

        self.collection_area = {"x_min":-0.6, "x_max":0.6, "y_min":0.9, "y_max":1.1, "object":"fruits"}
        self.weighing_area = {"x_min":-0.5, "x_max":0.5, "y_min":0.5, "y_max":0.7, "object":"scales",}
        self.accepted_fruit_pos = {"distance":0.8, "angle":-65*(numpy.pi/180)}
        self.rejected_fruit_pos = {"distance":0.8, "angle":65*(numpy.pi/180)}
        self.fruit_right_side_pos = {"distance":0.8, "angle":5*(numpy.pi/360)}
        self.fruit_left_side_pos = {"distance":0.8, "angle":-5*(numpy.pi/360)}

        self.fruit_correctly_accepted = False
        self.fruit_correctly_rejected = False

        self.gripper_max = 0.085

        self.iteration = 0
        self.change_reward_iterations = {}

        self.cbgroup_server=MutuallyExclusiveCallbackGroup()
        self.cbgroup_client=MutuallyExclusiveCallbackGroup()

        self.load_client=ServiceClient(LoadConfig, 'commander/load_experiment')

    def random_position(self, area):
        """
        Generate a random position within the specified area.

        :param area: The area where the position should be generated.
        :type area: dict
        :return: A tuple containing the distance and angle of the generated position.
        :rtype: tuple
        """
        valid = False
        while not valid:
            x = self.rng.uniform(low=area["x_min"], high=area["x_max"])
            y = self.rng.uniform(low=area["y_min"], high=area["y_max"])

            dist = numpy.linalg.norm([x,y])
            ang = numpy.arctan(x/y)

            valid = True
            
            for object in self.perceptions[area["object"]].data:
                if abs(object.distance - dist) < 0.1 and abs(object.angle - ang) < 0.09:
                    valid = False
                    break
            
        return dist, ang

    def generate_fruits(self, n_fruits, scale = None):
        """
        Generate a number of fruits with random positions and dimensions.

        :param n_fruits: Number of fruits to generate.
        :type n_fruits: int
        :param scale: If it is None, fruit will only be generated in the collection area. Otherwise, 
            fruits could be generated anywhere.
        :type scale: simulators_interfaces.msg.ScaleListMsg or NoneType
        """
        self.get_logger().info("Generating fruits...")
        for _ in range(n_fruits):
            distance, angle = self.random_position(self.collection_area)
            dim_max = self.rng.uniform(low=0.03, high=0.1)
            fruit = dict(distance = distance, angle = angle, dim_max = dim_max)
            self.fruits.append(fruit)

        if self.rng.uniform() > 0.5 and scale:
            if (self.iteration > self.change_reward_iterations['stage0']) and (self.iteration <= self.change_reward_iterations['stage1']):
                positions = ['accepted_pos', 'rejected_pos', 'scale_pos']
            else:
                positions = ['placed_pos_l', 'placed_pos_r', 'accepted_pos', 'rejected_pos', 'scale_pos']
            
            dim_max = self.rng.uniform(low=0.03, high=0.1)
            choice = self.rng.choice(positions)

            if choice == 'placed_pos_l':
                fruit = dict(
                    distance = self.fruit_left_side_pos['distance'],
                    angle = self.fruit_left_side_pos['angle'],
                    dim_max = dim_max    
                )
            elif choice == 'placed_pos_r':
                fruit = dict(
                    distance = self.fruit_right_side_pos['distance'],
                    angle = self.fruit_right_side_pos['angle'],
                    dim_max = dim_max    
                )
            elif choice == 'accepted_pos':
                fruit = dict(
                    distance = self.accepted_fruit_pos['distance'],
                    angle = self.accepted_fruit_pos['angle'],
                    dim_max = dim_max    
                )
            elif choice == 'rejected_pos':
                fruit = dict(
                    distance = self.rejected_fruit_pos['distance'],
                    angle = self.rejected_fruit_pos['angle'],
                    dim_max = dim_max    
                )
            elif choice == 'scale_pos':
                fruit = dict(
                    distance = scale.distance,
                    angle = scale.angle,
                    dim_max = dim_max    
                )
            
            self.fruits.append(fruit)



    def perceive_closest_fruit(self):
        """
        Choose the closest fruit from the list of fruits and update the fruit perceptions accordingly.
        Remember that, in this case, the robot is only able to perceive the fruit that is closest to it.
        """
        self.get_logger().info("Perceiving closest fruits...")
        if self.fruits:
            distances = numpy.array([fruit["distance"] for fruit in self.fruits])
            closest_fruit_index = numpy.argmin(distances)
            self.closest_fruit = self.fruits[closest_fruit_index]
            self.perceptions["fruits"].data[0].distance = self.closest_fruit["distance"]
            self.perceptions["fruits"].data[0].angle = self.closest_fruit["angle"]
            self.perceptions["fruits"].data[0].dim_max = self.closest_fruit["dim_max"]
        else:
            self.perceptions["fruits"].data[0].distance = 1.9
            self.perceptions["fruits"].data[0].angle = 1.4
            self.perceptions["fruits"].data[0].dim_max = 0.1
            self.closest_fruit = None

    def random_perceptions(self):
        """
        Generate random perceptions when the world is reset.
        """
        self.catched_fruit = None

        # Generate scale
        self.perceptions["scales"].data = []
        self.perceptions["scales"].data.append(self.base_messages["scales"]())
        distance, angle = self.random_position(self.weighing_area)
        self.perceptions["scales"].data[0].distance = distance
        self.perceptions["scales"].data[0].angle = angle
        self.perceptions["scales"].data[0].state = 0
        self.perceptions["scales"].data[0].active = False
        # Generate fruits
        self.fruits = []
        n_fruits = self.rng.integers(0,4)
        self.perceptions["fruits"].data = []
        self.perceptions["fruits"].data.append(self.base_messages["fruits"]())

        self.generate_fruits(n_fruits, self.perceptions['scales'].data[0])

        self.perceive_closest_fruit()

        
        self.perceptions["fruit_in_right_hand"].data = False
        self.perceptions["fruit_in_left_hand"].data = False
        if self.rng.uniform() > 0.5 and self.fruits:
            if self.perceptions["fruits"].data[0].angle > 0.0:
                self.perceptions["fruit_in_right_hand"].data = True
                self.perceptions["fruit_in_left_hand"].data = False
            else:
                self.perceptions["fruit_in_left_hand"].data = True
                self.perceptions["fruit_in_right_hand"].data = False

            self.catched_fruit = self.closest_fruit

        self.perceptions["button_light"].data = False if self.rng.uniform() > 0.5 else True

        self.update_reward_sensor()
    
    def pick_fruit_policy(self):
        """
        Pick the closest fruit to the robot.

        :raises RuntimeError: If the closest fruit is not the fruit on the scale when the scale is active.
        """
        scale = self.perceptions["scales"].data[0]
        if not self.catched_fruit and self.fruits:
            if self.closest_fruit["angle"] > 0.0:
                self.perceptions["fruit_in_right_hand"].data = True
            else:
                self.perceptions["fruit_in_left_hand"].data = True
            if scale.active:
                scale.active = False
                #scale.state = 0
                if self.closest_fruit == self.tested_fruit:
                    self.tested_fruit = None
                else:
                    raise RuntimeError("The tested fruit should be the closest fruit!!")
            self.fruit_correctly_rejected = False
            self.fruit_correctly_accepted = False
            self.catched_fruit = self.closest_fruit
    
    def change_hands_policy(self):
        """
        Change the fruit from one gripper to the other if the fruit is small enough.
        """
        if self.catched_fruit:
            if self.catched_fruit["dim_max"] <= self.gripper_max:
                if self.perceptions["fruit_in_left_hand"].data:
                    self.perceptions["fruit_in_left_hand"].data = False
                    self.perceptions["fruit_in_right_hand"].data = True
                    self.catched_fruit["angle"] = -self.catched_fruit["angle"]
                elif self.perceptions["fruit_in_right_hand"].data:
                    self.perceptions["fruit_in_left_hand"].data = True
                    self.perceptions["fruit_in_right_hand"].data = False
                    self.catched_fruit["angle"] = -self.catched_fruit["angle"]
    
    def place_fruit_policy(self):
        """
        Place the fruit in the center of the table.
        If the fruit is in the left hand, it will be placed slightly on the right 
        side of the table, and vice versa.
        It is a way to change the side of the table where the fruit is placed.
        """
        if self.catched_fruit:
            if self.perceptions["fruit_in_left_hand"].data:
                self.catched_fruit["distance"] = self.fruit_right_side_pos["distance"]
                self.catched_fruit["angle"] = self.fruit_right_side_pos["angle"]
                self.perceptions["fruit_in_left_hand"].data = False
                self.catched_fruit = None

            elif self.perceptions["fruit_in_right_hand"].data:
                self.catched_fruit["distance"] = self.fruit_left_side_pos["distance"]
                self.catched_fruit["angle"] = self.fruit_left_side_pos["angle"]
                self.perceptions["fruit_in_right_hand"].data = False
                self.catched_fruit = None

    def test_fruit_policy(self):
        """
        Put the fruit on the scale in order to test it.
        """
        if self.catched_fruit:
            scale = self.perceptions["scales"].data[0]
            if (self.perceptions["fruit_in_left_hand"].data and scale.angle <= 0.0) or (
                self.perceptions["fruit_in_right_hand"].data and scale.angle > 0.0):
                self.catched_fruit["distance"] = scale.distance
                self.catched_fruit["angle"] = scale.angle
                if self.iteration > self.change_reward_iterations['stage1']:
                    scale.active = True
                    if scale.state == 0:
                        scale.state = 1 if self.rng.uniform() > 0.5 else 2
                self.perceptions["fruit_in_left_hand"].data = False
                self.perceptions["fruit_in_right_hand"].data = False
                self.tested_fruit = self.catched_fruit
                self.catched_fruit = None

    def ask_nicely_policy(self):
        """
        Ask the experimenter to provide more fruits if there are none available.
        """
        if not self.fruits:
            n_fruits = self.rng.integers(1,4)
            self.generate_fruits(n_fruits, None)

    def accept_fruit_policy(self):
        """
        Put the fruit into the accepted fruit box if the fruit is valid.
        """
        scale = self.perceptions["scales"].data[0]
        if scale.active:
            self.tested_fruit["distance"] = self.accepted_fruit_pos["distance"]
            self.tested_fruit["angle"] = self.accepted_fruit_pos["angle"]
            if scale.state == 1:
                self.fruit_correctly_accepted = True
                scale.state = 0
            scale.active = False
            self.tested_fruit = None

        elif self.catched_fruit:
            if self.perceptions["fruit_in_left_hand"].data:
                self.catched_fruit["distance"] = self.accepted_fruit_pos["distance"]
                self.catched_fruit["angle"] = self.accepted_fruit_pos["angle"]
                self.perceptions["fruit_in_left_hand"].data = False
                self.catched_fruit = None

    
    def discard_fruit_policy(self):
        """
        Put the fruit into the discarded fruit box if the fruit is not valid.
        """
        scale = self.perceptions["scales"].data[0]
        if scale.active:
            self.tested_fruit["distance"] = self.rejected_fruit_pos["distance"]
            self.tested_fruit["angle"] = self.rejected_fruit_pos["angle"]
            if scale.state == 2:
                self.fruit_correctly_rejected = True
                scale.state = 0
            scale.active = False
            self.tested_fruit = None
        elif self.catched_fruit:
            if self.perceptions["fruit_in_right_hand"].data:
                self.catched_fruit["distance"] = self.rejected_fruit_pos["distance"]
                self.catched_fruit["angle"] = self.rejected_fruit_pos["angle"]
                self.perceptions["fruit_in_right_hand"].data = False
                self.catched_fruit = None

    
    def press_button_policy(self):
        """
        Toggle the light of the button.
        """
        if self.perceptions["button_light"].data:
            self.perceptions["button_light"].data = False
        else:
            self.perceptions["button_light"].data = True

    def fruit_in_placed_pos(self):
        """
        Check if the fruit is in the center of the table.

        :return: True if the fruit is in the center of the table, False otherwise.
        :rtype: bool
        """
        fruit = self.perceptions['fruits'].data[0]

        placed = (fruit.distance == self.fruit_left_side_pos['distance']) and (
            abs(fruit.angle) == abs(self.fruit_left_side_pos['angle'])) and (
            not self.catched_fruit)
        
        return placed
        

    def reward_progress_classify_fruit_goal(self):
        """
        Gives a larger reward the closer the robot is to correctly classifying a fruit.
        If the fruit is correctly accepted or discarded, the reward is 1.0.
        """
        scale = self.perceptions["scales"].data[0]
        fruit = self.perceptions["fruits"].data[0]
        progress = 0.0
        if self.iteration > self.change_reward_iterations['stage2']:
            self.get_logger().info("STAGE 2 REWARD: PROGRESS CLASSIFY FRUIT")
            if self.fruit_correctly_accepted or self.fruit_correctly_rejected:
                progress = 1.0
            
            elif scale.active==True:
                progress = 0.75
            
            elif self.catched_fruit:
                if (fruit.angle * scale.angle) > 0:
                    progress = 0.5
                else:
                    progress = 0.250
            
            elif not self.catched_fruit:
                if abs(fruit.angle) == abs(self.fruit_left_side_pos["angle"]) and (
                    fruit.distance == self.fruit_left_side_pos["distance"]) and (
                        fruit.angle * scale.angle > 0
                    ):
                    progress = 0.375
            
                elif self.fruits:
                    progress = 0.125

            self.perceptions["progress_classify_fruit_goal"].data = progress


    def reward_place_fruit_goal(self):
        """
        Gives a reward of 1.0 if the fruit is placed in the center of the table.
        """
        reward = 0.0 
        if (self.iteration > self.change_reward_iterations['stage0']) and (self.iteration <= self.change_reward_iterations['stage1']):
            self.get_logger().info("STAGE 1 REWARD: PLACE FRUIT")
            if self.fruit_in_placed_pos():
                reward = 1.0
                
        else:
            reward = 1.0
            if (self.iteration > self.change_reward_iterations['stage0']) and (self.iteration <= self.change_reward_iterations['stage2']):
                self.get_logger().info("STAGE 2 REWARD: NONE")
            else:
                self.get_logger().info("STAGE 0 REWARD: NONE")
            
        self.perceptions["place_fruit_goal"].data = reward

    def reward_classify_fruit_goal(self):
        """
        Gives a reward of 1.0 if the fruit is correctly classified.
        """
        reward = 0.0
        if self.iteration > self.change_reward_iterations['stage2']:
            self.get_logger().info("STAGE 2 REWARD: CLASSIFY FRUIT")
            if self.fruit_correctly_accepted or self.fruit_correctly_rejected:
                reward = 1.0
        self.perceptions["classify_fruit_goal"].data = reward

    def reset_world(self, data):
        """
        Reset the world to a new state.

        :param data: The message that contains the command to reset the world. It is not used.
        :type data: ROS msg defined in the config file. Typically cognitive_processes_interfaces.msg.ControlMsg or
            cognitive_processes_interfaces.srv.WorldReset.Request
        """
        self.get_logger().info("Resetting world...")
        self.fruit_correctly_accepted = False
        self.fruit_correctly_rejected = False
        self.random_perceptions()
        self.publish_perceptions()
        if (not self.catched_fruit) and (
            self.perceptions["fruit_in_left_hand"].data
            or self.perceptions["fruit_in_right_hand"].data
        ):
            self.get_logger().error("Critical error: catched_object is empty and it should not!!!")

    def update_reward_sensor(self):
        """
        Update goal sensors' values.
        """
        for sensor in self.perceptions:
            reward_method = getattr(self, "reward_" + sensor, None)
            if callable(reward_method):
                reward_method()
    
    def publish_perceptions(self):
        """
        Publish the current perceptions to the corresponding topics.
        """
        for ident, publisher in self.sim_publishers.items():
            self.get_logger().debug("Publishing " + ident + " = " + str(self.perceptions[ident].data))
            publisher.publish(self.perceptions[ident])

    def world_reset_service_callback(self, request, response):
        """
        Callback for the world reset service.

        :param request: The message that contains the request to reset the world.
        :type request: ROS msg defined in the config file Typically cognitive_processes_interfaces.srv.WorldReset.Request
        :param response: Response of the world reset service.
        :type response: ROS msg defined in the config file. Typically cognitive_processes_interfaces.srv.WorldReset.Response
        :return: Response indicating the success of the world reset.
        :rtype: ROS msg defined in the config file. Typically cognitive_processes_interfaces.srv.WorldReset.Response
        """
        self.reset_world(request)
        response.success=True
        return response

    def new_command_callback(self, data):
        """
        Process a command received

        :param data: The message that contais the command received.
        :type data: ROS msg defined in the config file. Typically cognitive_processes_interfaces.msg.ControlMsg
        """
        self.get_logger().debug(f"Command received... ITERATION: {data.iteration}")
        self.iteration = data.iteration
        self.update_reward_sensor()
        if data.command == "reset_world":
            self.reset_world(data)
        elif data.command == "end":
            self.get_logger().info("Ending simulator as requested by LTM...")
            rclpy.shutdown()

    def new_action_service_callback(self, request, response):
        """
        Execute a policy and publish new perceptions.

        :param request: The message that contains the policy to execute.
        :type request: ROS srv defined in the config file. Typically cognitive_node_interfaces.srv.Policy.Request
        :param response: Response of the success of the execution of the action.
        :type response: ROS srv defined in the config file. Typically cognitive_node_interfaces.srv.Policy.Response
        :return: Response indicating the success of the action execution.
        :rtype: ROS srv defined in the config file. Typically cognitive_node_interfaces.srv.Policy.Response
        """
        self.get_logger().info("Executing policy " + str(request.policy))
        self.get_logger().info(f"ITERATION: {self.iteration}")
        self.perceive_closest_fruit()
        self.get_logger().info(f"FRUITS BEFORE POLICY: {self.fruits}")
        self.get_logger().info(f"CATCHED FRUIT BEFORE: {self.catched_fruit}")
        self.get_logger().info(f"PERCEPTIONS BEFORE: {self.perceptions}")
        self.get_logger().info(f"POLICY TO EXECUTE: {request.policy}")
        getattr(self, request.policy + "_policy")()
        self.perceive_closest_fruit()
        self.get_logger().info(f"FRUITS AFTER POLICY: {self.fruits}")
        self.get_logger().info(f"CATCHED FRUIT AFTER: {self.catched_fruit}")
        self.get_logger().info(f"PERCEPTIONS AFTER: {self.perceptions}")
        self.update_reward_sensor()
        self.publish_perceptions()
        if (not self.catched_fruit) and (
            self.perceptions["fruit_in_left_hand"].data
            or self.perceptions["fruit_in_right_hand"].data
        ):
            self.get_logger().error("Critical error: catched_object is empty and it should not!!!")
            rclpy.shutdown()
        response.success = True
        return response

    def setup_experiment_stages(self, stages):
        """
        Setup the stages of the experiment with their corresponding iterations.

        :param stages: A dictionary where keys are stage names and values are the iterations at which the stage starts.
        :type stages: dict
        """
        for stage in stages:
            self.change_reward_iterations[stage] = stages[stage]

    def setup_perceptions(self, perceptions):
        """
        Configure the ROS topics where the simulator will publish the perceptions.

        :param perceptions: A list of dictionaries where each dictionary contains the name, perception topic, and perception message class.
        :type perceptions: list
        """
        for perception in perceptions:
            sid = perception["name"]
            topic = perception["perception_topic"]
            classname = perception["perception_msg"]
            message = class_from_classname(classname)
            self.perceptions[sid] = message()
            if "List" in classname:
                self.perceptions[sid].data = []
                self.base_messages[sid] = class_from_classname(classname.replace("List", ""))
            elif "Float" in classname:
                self.perceptions[sid].data = 0.0
            else:
                self.perceptions[sid].data = False
            self.get_logger().info("I will publish to... " + str(topic))
            self.sim_publishers[sid] = self.create_publisher(message, topic, 0)

    def setup_control_channel(self, simulation):
        """
        Configure the ROS topic/service where listen for commands to be executed.

        :param simulation: The params from the config file to setup the control channel.
        :type simulation: dict
        """
        self.ident = simulation["id"]
        topic = simulation["control_topic"]
        classname = simulation["control_msg"]
        message = class_from_classname(classname)
        self.get_logger().info("Subscribing to... " + str(topic))
        self.create_subscription(message, topic, self.new_command_callback, 0)
        service_policy = simulation.get("executed_policy_service")
        service_world_reset = simulation.get("world_reset_service")

        if service_policy:
            self.get_logger().info("Creating server... " + str(service_policy))
            classname = simulation["executed_policy_msg"]
            message_policy_srv = class_from_classname(classname)
            self.create_service(message_policy_srv, service_policy, self.new_action_service_callback, callback_group=self.cbgroup_server)
            self.get_logger().info("Creating perception publisher timer... ")
            self.perceptions_timer = self.create_timer(0.01, self.publish_perceptions, callback_group=self.cbgroup_server)

        if service_world_reset:
            self.message_world_reset = class_from_classname(simulation["world_reset_msg"])
            self.create_service(self.message_world_reset, service_world_reset, self.world_reset_service_callback, callback_group=self.cbgroup_server)     

    def load_experiment_file_in_commander(self):
        """
        Load the configuration file in the commander node.

        :return: Response from the commander node indicating the success of the loading.
        :rtype: core_interfaces.srv.LoadConfig.Response
        """
        loaded = self.load_client.send_request(file = self.config_file)
        return loaded

    def load_configuration(self):
        """
        Load the configuration file and setup the simulator.
        It is configured the random number generator, the stages of the experiment,
        the perceptions, and the control channel.
        """
        if self.random_seed:
            self.rng = numpy.random.default_rng(self.random_seed)
            self.get_logger().info(f"Setting random number generator with seed {self.random_seed}")
        else:
            self.rng = numpy.random.default_rng()

        if self.config_file is None:
            self.get_logger().error("No configuration file for the LTM simulator specified!")
            rclpy.shutdown()
        else:
            if not os.path.isfile(self.config_file):
                self.get_logger().error(self.config_file + " does not exist!")
                rclpy.shutdown()
            else:
                self.get_logger().info(f"Loading configuration from {self.config_file}...")
                config = yaml.load(
                    open(self.config_file, "r", encoding="utf-8"),
                    Loader=yamlloader.ordereddict.CLoader,
                )
                self.setup_experiment_stages(config["DiscreteEventSimulator"]["Stages"])
                self.setup_perceptions(config["DiscreteEventSimulator"]["Perceptions"])
                # Be ware, we can not subscribe to control channel before creating all sensor publishers.
                self.setup_control_channel(config["Control"])
        
        self.load_experiment_file_in_commander()

def main(args=None):
    rclpy.init(args=args)
    sim = FruitShopSim()
    sim.load_configuration()

    try:
        rclpy.spin(sim)
    except KeyboardInterrupt:
        print('Keyboard Interrupt Detected: Shutting down simulator...')
    finally:
        sim.destroy_node()

if __name__ == '__main__':
    main()       

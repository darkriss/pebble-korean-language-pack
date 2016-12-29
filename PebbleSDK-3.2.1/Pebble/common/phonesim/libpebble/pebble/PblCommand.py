import os
import logging

class PblCommand:
    name = ''
    help = ''

    def run(args):
        pass

    def configure_subparser(self, parser):
        parser.add_argument('--sdk', help='Path to Pebble SDK (ie: ~/pebble-dev/PebbleSDK-2.X/)')
        parser.add_argument('--debug', action='store_true',
                help='Enable debugging output')

    def sdk_path(self, args):
        """
        Tries to guess the location of the Pebble SDK
        """
        env_sdk_path = os.getenv('PEBBLE_SDK_PATH')
        if args.sdk:
            sdk_path = args.sdk
        elif env_sdk_path:
            sdk_path = env_sdk_path
            logging.info("Overriding Pebble SDK Path with '%s'", sdk_path)
        else:
            sdk_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..'))
        if not sdk_path:
            raise Exception("SDK path undefined!")
        if not os.path.exists(sdk_path):
            raise Exception("SDK path '{}' doesn't exist!".format(sdk_path))
        return sdk_path

    def add_arm_tools_to_path(self, args):
        os.environ['PATH'] = "{}:{}".format(
            os.environ['PATH'],
            os.path.join(self.sdk_path(args), "arm-cs-tools", "bin"))

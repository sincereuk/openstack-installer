#
# Copyright 2014 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import urwid
import os

import time
from cloudinstall.config import Config
from cloudinstall.core import DisplayController
from cloudinstall.multi_install import (MultiInstallNewMaas,
                                        MultiInstallExistingMaas)
from cloudinstall import utils


log = logging.getLogger('cloudinstall.install')


class SingleInstall:

    def __init__(self, opts, ui):
        self.opts = opts
        self.ui = ui
        self.config = Config()
        self.container_name = 'uoi-bootstrap'
        self.container_path = '/var/lib/lxc'
        self.container_abspath = os.path.join(self.container_path,
                                              self.container_name)
        self.userdata = os.path.join(
            self.config.cfg_path, 'userdata.yaml')

        # Sets install type
        utils.spew(os.path.join(self.config.cfg_path, 'single'),
                   'auto-generated')

    def prep_userdata(self):
        """ preps userdata file for container install """
        dst_file = os.path.join(self.config.cfg_path,
                                'userdata.yaml')
        original_data = utils.load_template('userdata.yaml')
        modified_data = original_data.render(
            extra_sshkeys=[utils.ssh_readkey()],
            extra_pkgs=['juju-local'])
        utils.spew(dst_file, modified_data)

    def create_container_and_wait(self):
        """ Creates container and waits for cloud-init to finish
        """
        utils.container_create(self.container_name, self.userdata)
        utils.container_start(self.container_name)
        utils.container_wait(self.container_name)
        tries = 1
        while not self.cloud_init_finished():
            self.ui.info_message("[{0}] * Waiting for container to finalize, "
                                 "please wait ...       ".format(tries))
            time.sleep(1)
            tries = tries + 1

    def cloud_init_finished(self):
        """ checks the log to see if cloud-init finished
        """
        log_file = os.path.join(self.container_abspath,
                                'rootfs/var/log/cloud-init-output.log')
        out = utils.get_command_output('sudo tail -n1 {0}'.format(log_file))
        if 'finished at' in out['output']:
            return True
        return False

    def copy_installdata_and_set_perms(self):
        """ copies install data and sets permissions on files/dirs
        """
        utils.get_command_output("chown {0}:{0} -R {1}".format(
            utils.install_user(), self.config.cfg_path))

        # copy over the rest of our installation data from host
        # and setup permissions

        utils.container_run(self.container_name, 'mkdir -p ~/.cloud-install')
        utils.container_run(
            self.container_name, 'sudo mkdir -p /etc/openstack')

        utils.container_cp(self.container_name,
                           os.path.join(
                               utils.install_home(), '.cloud-install/*'),
                           '.cloud-install/.')

        # our ssh keys too
        utils.container_cp(self.container_name,
                           os.path.join(utils.install_home(),
                                        '.ssh/id_rsa*'),
                           '.ssh/.')
        utils.container_run(self.container_name, "chmod 600 .ssh/id_rsa*")

    def run(self):
        if os.path.exists(self.container_abspath):
            # Container exists, handle return code in installer
            raise SystemExit("Container exists, please uninstall or kill "
                             "existing cloud before proceeding.")

        self.ui.info_message(
            "* Please wait while we generate your isolated environment ...")

        utils.ssh_genkey()

        # Prepare cloud-init file for creation
        self.prep_userdata()

        # Start container
        self.create_container_and_wait()

        # configure juju environment for bootstrap
        single_env = utils.load_template('juju-env/single.yaml')
        single_env_modified = single_env.render(
            openstack_password=self.config.openstack_password)
        utils.spew('/tmp/single.yaml', single_env_modified)
        utils.container_run(self.container_name,
                            'mkdir -p .juju')
        utils.container_cp(self.container_name,
                           '/tmp/single.yaml',
                           '.juju/environments.yaml')

        # Set permissions
        self.copy_installdata_and_set_perms()

        # setup charm confingurations
        charm_conf = utils.load_template('charmconf.yaml')
        charm_conf_modified = charm_conf.render(
            openstack_password=self.config.openstack_password)
        utils.spew(os.path.join(self.config.cfg_path,
                                'charmconf.yaml'),
                   charm_conf_modified)

        # start the party
        cloud_status_bin = ['cloud-status']
        if self.opts.enable_swift:
            cloud_status_bin.append('--enable-swift')
        self.ui.info_message("Bootstrapping Juju ..")
        utils.container_run(self.container_name, "juju bootstrap")
        utils.container_run(self.container_name, "juju status")
        self.ui.info_message("Starting cloud deployment ..")
        utils.container_run_status(
            self.container_name, " ".join(cloud_status_bin))


class LandscapeInstall:

    def __init__(self, opts, ui):
        self.config = Config()
        self.opts = opts
        self.ui = ui

    def run(self):
        raise NotImplementedError("Landscape install not implemented.")


class InstallController(DisplayController):
    """ Install controller """

    def __init__(self, **kwds):
        super().__init__(**kwds)

    def _save_password(self, password=None, confirm_pass=None):
        """ Checks passwords match and proceeds
        """
        if password and password == confirm_pass:
            self.config.save_password(password)
            self.ui.hide_show_password_input()
            self.select_install_type()
        else:
            self.error_message('Passwords did not match, try again ..')
            return self.show_password_input(
                'Openstack Password', self._save_password)

    def select_install_type(self):
        """ Dialog for selecting installation type
        """
        self.info_message("Choose your installation path ..")
        self.show_selector_info('Install Type',
                                self.config.install_types,
                                self.do_install)

    def main_loop(self):
        if not hasattr(self, 'loop'):
            self.loop = urwid.MainLoop(self.ui,
                                       self.config.STYLES,
                                       handle_mouse=True,
                                       unhandled_input=self.header_hotkeys)

        self.info_message("Get started by entering an Openstack password "
                          "to use in your cloud ..")
        self.ui.show_password_input(
            'Openstack Password', self._save_password)
        self.loop.run()

    def do_install(self, install_type):
        """ Callback for install type selector
        """
        self.ui.hide_selector_info()
        if 'Single' in install_type:
            SingleInstall(self.opts, self).run()
        elif 'Multi with existing MAAS' == install_type:
            MultiInstallExistingMaas(self.opts, self).run()
        elif 'Multi' == install_type:
            MultiInstallNewMaas(self.opts, self).run()
        else:
            LandscapeInstall(self.opts, self).run()

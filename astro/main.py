"""
An astronomical plugin, for providing the system with astronomical data (e.g. whether it's day or not, based on the sun's location)
"""

import re
import sys
import time
import requests
import simplejson as json
from threading import Thread, Event
from datetime import datetime, timedelta
from plugins.base import om_expose, background_task, OMPluginBase, PluginConfigChecker
import _strptime # Fix error with implicit import on different thread


class Astro(OMPluginBase):
    """
    An astronomical plugin, for providing the system with astronomical data (e.g. whether it's day or not, based on the sun's location)
    """

    name = 'Astro'
    version = '0.6.5'
    interfaces = [('config', '1.0')]

    config_description = [{'name': 'location',
                           'type': 'str',
                           'description': 'A written location to be translated to coordinates using Google. Leave empty and provide coordinates below to prevent using the Google services.'},
                          {'name': 'coordinates',
                           'type': 'str',
                           'description': 'Coordinates in the form of `lat;long` where both are a decimal numbers with dot as decimal separator. Leave empty to fill automatically using the location above.'},
                          {'name': 'horizon_bit',
                           'type': 'int',
                           'description': 'The bit that indicates whether it is day. -1 when not in use.'},
                          {'name': 'civil_bit',
                           'type': 'int',
                           'description': 'The bit that indicates whether it is day or civil twilight. -1 when not in use.'},
                          {'name': 'nautical_bit',
                           'type': 'int',
                           'description': 'The bit that indicates whether it is day, civil or nautical twilight. -1 when not in use.'},
                          {'name': 'astronomical_bit',
                           'type': 'int',
                           'description': 'The bit that indicates whether it is day, civil, nautical or astronomical twilight. -1 when not in use.'},
                          {'name': 'bright_bit',
                           'type': 'int',
                           'description': 'The bit that indicates the brightest part of the day. -1 when not in use.'},
                          {'name': 'bright_offset',
                           'type': 'int',
                           'description': 'The offset (in minutes) after sunrise and before sunset on which the bright_bit should be set.'},
                          {'name': 'group_action',
                           'type': 'int',
                           'description': 'The ID of a Group Action to be called when another zone is entered. -1 when not in use.'}]

    default_config = {'location': 'Brussels,Belgium',
                      'horizon_bit': -1,
                      'civil_bit': -1,
                      'nautical_bit': -1,
                      'astronomical_bit': -1,
                      'bright_bit': -1,
                      'bright_offset': 60,
                      'group_action': -1}

    def __init__(self, webinterface, logger):
        super(Astro, self).__init__(webinterface, logger)
        self.logger('Starting Astro plugin...')

        self._config = self.read_config(Astro.default_config)
        self._config_checker = PluginConfigChecker(Astro.config_description)

        self._bright_bit = -1
        self._horizon_bit = -1
        self._civil_bit = -1
        self._nautical_bit = -1
        self._astronomical_bit = -1
        self._previous_bits = [None, None, None, None, None]
        self._sleeper = Event()
        self._sleep_until = 0

        thread = Thread(target=self._sleep_manager)
        thread.start()

        self._read_config()

        self.logger("Started Astro plugin")

    def _read_config(self):
        for bit in ['bright_bit', 'horizon_bit', 'civil_bit', 'nautical_bit', 'astronomical_bit']:
            try:
                value = int(self._config.get(bit, Astro.default_config[bit]))
            except ValueError:
                value = Astro.default_config[bit]
            setattr(self, '_{0}'.format(bit), value)
        try:
            self._bright_offset = int(self._config.get('bright_offset', Astro.default_config['bright_offset']))
        except ValueError:
            self._bright_offset = Astro.default_config['bright_offset']
        try:
            self._group_action = int(self._config.get('group_action', Astro.default_config['group_action']))
        except ValueError:
            self._group_action = Astro.default_config['group_action']

        self._previous_bits = [None, None, None, None, None]
        self._coordinates = None
        self._enabled = False

        coordinates = self._config.get('coordinates', '').strip()
        match = re.match(r'^(\d+\.\d+);(\d+\.\d+)$', coordinates)
        if match:
            self._latitude = match.group(1)
            self._longitude = match.group(2)
            self._enable_plugin()
        else:
            thread = Thread(target=self._translate_address)
            thread.start()
            self.logger('Astro is disabled')

    def _translate_address(self):
        wait = 0
        location = self._config.get('location', '').strip()
        if not location:
            self.logger('No coordinates and no location. Please fill in one of both to enable the Astro plugin.')
            return
        while True:
            api = 'https://maps.googleapis.com/maps/api/geocode/json?address={0}'.format(location)
            try:
                coordinates = requests.get(api).json()
                if coordinates['status'] == 'OK':
                    self._latitude = coordinates['results'][0]['geometry']['location']['lat']
                    self._longitude = coordinates['results'][0]['geometry']['location']['lng']
                    self._config['coordinates'] = '{0};{1}'.format(self._latitude, self._longitude)
                    self.write_config(self._config)
                    self._enable_plugin()
                    return
                error = coordinates['status']
            except Exception as ex:
                error = ex.message
            if wait == 0:
                wait = 1
            elif wait == 1:
                wait = 5
            elif wait < 60:
                wait = wait + 5
            self.logger('Error calling Google Maps API, waiting {0} minutes to try again: {1}'.format(wait, error))
            time.sleep(wait * 60)
            if self._enabled is True:
                return  # It might have been set in the mean time

    def _enable_plugin(self):
        now = datetime.utcnow()
        self.logger('Latitude: {0} - Longitude: {1}'.format(self._latitude, self._longitude))
        self.logger('It\'s now {0} UTC'.format(now.strftime('%Y-%m-%d %H:%M:%S')))
        self.logger('Astro is enabled')
        self._enabled = True
        # Trigger complete recalculation
        self._previous_bits = [None, None, None, None, None]
        self._sleep_until = 0

    def _sleep_manager(self):
        while True:
            if not self._sleeper.is_set() and self._sleep_until < time.time():
                self._sleeper.set()
            time.sleep(5)

    def _sleep(self, timestamp):
        self._sleep_until = timestamp
        self._sleeper.clear()
        self._sleeper.wait()

    def get_sun_data_or_cached(self):
        try:
            self.logger("getting sun data")
            local_now = datetime.now()
            data = requests.get('http://api.sunrise-sunset.org/json?lat={0}&lng={1}&date={2}&formatted=0'.format(
                self._latitude, self._longitude, local_now.strftime('%Y-%m-%d')
            )).json()
            self.logger('sun data received')
            self.sunrise_data = data
        except Exception as ex:
            self.logger('Error requesting sun data: {0}'.format(ex))
        return self.sunrise_data

    @staticmethod
    def _convert(dt_string):
        date = datetime.strptime(dt_string, '%Y-%m-%dT%H:%M:%S+00:00')
        if date.year == 1970:
            return None
        return date

    @staticmethod
    def _seconds_to_tomorrow():
        local_now = datetime.now()
        local_tomorrow = datetime(local_now.year, local_now.month, local_now.day) + timedelta(days=1)
        return int((local_tomorrow - local_now).total_seconds())

    @background_task
    def run(self):
        self._previous_bits = [None, None, None, None, None]
        while True:
            if self._enabled:
                now = datetime.utcnow()
                self.logger('It\'s now {0} UTC'.format(now.strftime('%Y-%m-%d %H:%M:%S')))
                try:
                    data = self.get_sun_data_or_cached()
                    self.logger(data['status'])
                    sleep = 24 * 60 * 60
                    bits = [True, True, True, True, True]  # ['bright', day, civil, nautical, astronomical]
                    if data['status'] == 'OK':
                        # Load data
                        sunrise = Astro._convert(data['results']['sunrise'])
                        sunset = Astro._convert(data['results']['sunset'])
                        has_sun = sunrise is not None and sunset is not None
                        if has_sun is True:
                            bright_beg = sunrise + timedelta(minutes=self._bright_offset)
                            bright_end = sunset - timedelta(minutes=self._bright_offset)
                            has_bright = bright_beg < bright_end
                        else:
                            has_bright = False
                        civil_beg = Astro._convert(data['results']['civil_twilight_begin'])
                        civil_end = Astro._convert(data['results']['civil_twilight_end'])
                        has_civil = civil_beg is not None and civil_end is not None
                        nautical_beg = Astro._convert(data['results']['nautical_twilight_begin'])
                        nautical_end = Astro._convert(data['results']['nautical_twilight_end'])
                        has_nautical = nautical_beg is not None and nautical_end is not None
                        astronomical_beg = Astro._convert(data['results']['astronomical_twilight_begin'])
                        astronomical_end = Astro._convert(data['results']['astronomical_twilight_end'])
                        has_astronomical = astronomical_beg is not None and astronomical_end is not None
                        self.logger("astro beg: {} UTC".format(astronomical_beg))
                        self.logger("nauti beg: {} UTC".format(nautical_beg))
                        self.logger("civil beg: {} UTC".format(civil_beg))
                        self.logger("sunrise:   {} UTC".format(sunrise))
                        self.logger("bright_beg:{} UTC".format(bright_beg))
                        self.logger("bright_end:{} UTC".format(bright_end))
                        self.logger("sunset:    {} UTC".format(sunset))
                        self.logger("civil end: {} UTC".format(civil_end))
                        self.logger("nauti end: {} UTC".format(nautical_end))
                        self.logger("astro end: {} UTC".format(astronomical_end))
                        self.logger("now: {} UTC".format(now))
                        # Analyse data
                        if not any([has_sun, has_civil, has_nautical, has_astronomical]):
                            # This is an educated guess; Polar day (sun never sets) and polar night (sun never rises) can
                            # happen in the polar circles. However, since we have far more "gradients" in the night part,
                            # polar night (as defined here - pitch black) only happens very close to the poles. So it's
                            # unlikely this plugin is used there.
                            info = 'polar day'
                            bits = [True, True, True, True, True]
                            sleep = Astro._seconds_to_tomorrow()
                        else:
                            if has_bright is False:
                                bits[0] = False
                            else:
                                bits[0] = bright_beg < now < bright_end
                                if bits[0] is True:
                                    sleep = min(sleep, int((bright_end - now).total_seconds()))
                                elif now < bright_beg:
                                    sleep = min(sleep, int((bright_beg - now).total_seconds()))
                            if has_sun is False:
                                bits[1] = False
                            else:
                                bits[1] = sunrise < now < sunset
                                if bits[1] is True:
                                    sleep = min(sleep, (sunset - now).total_seconds())
                                elif now < sunrise:
                                    sleep = min(sleep, (sunrise - now).total_seconds())
                            if has_civil is False:
                                if has_sun is True:
                                    bits[2] = not bits[1]
                                else:
                                    bits[2] = False
                            else:
                                bits[2] = civil_beg < now < civil_end
                                if bits[2] is True:
                                    sleep = min(sleep, (civil_end - now).total_seconds())
                                elif now < sunrise:
                                    sleep = min(sleep, (civil_beg - now).total_seconds())
                            if has_nautical is False:
                                if has_sun is True or has_civil is True:
                                    bits[3] = not bits[2]
                                else:
                                    bits[3] = False
                            else:
                                bits[3] = nautical_beg < now < nautical_end
                                if bits[3] is True:
                                    sleep = min(sleep, (nautical_end - now).total_seconds())
                                elif now < sunrise:
                                    sleep = min(sleep, (nautical_beg - now).total_seconds())
                            if has_astronomical is False:
                                if has_sun is True or has_civil is True or has_nautical is True:
                                    bits[4] = not bits[3]
                                else:
                                    bits[4] = False
                            else:
                                bits[4] = astronomical_beg < now < astronomical_end
                                if bits[4] is True:
                                    sleep = min(sleep, (astronomical_end - now).total_seconds())
                                elif now < sunrise:
                                    sleep = min(sleep, (astronomical_beg - now).total_seconds())
                            sleep = min(sleep, Astro._seconds_to_tomorrow())
                            info = 'night'
                            if bits[4] is True:
                                info = 'astronomical twilight'
                            if bits[3] is True:
                                info = 'nautical twilight'
                            if bits[2] is True:
                                info = 'civil twilight'
                            if bits[1] is True:
                                info = 'day'
                            if bits[0] is True:
                                info = 'day (bright)'
                        # Set bits in system
                        for index, bit in {0: self._bright_bit,
                                           1: self._horizon_bit,
                                           2: self._civil_bit,
                                           3: self._nautical_bit,
                                           4: self._astronomical_bit}.iteritems():
                            if bit > -1:
                                result = json.loads(self.webinterface.do_basic_action(None, 237 if bits[index] else 238, bit))
                                if result['success'] is False:
                                    self.logger('Failed to set bit {0} to {1}'.format(bit, 1 if bits[index] else 0))
                        if self._previous_bits != bits:
                            if self._group_action != -1:
                                result = json.loads(self.webinterface.do_basic_action(None, 2, self._group_action))
                                if result['success'] is True:
                                    self.logger('Group Action {0} triggered'.format(self._group_action))
                                else:
                                    self.logger('Failed to trigger Group Action {0}'.format(self._group_action))
                            self._previous_bits = bits
                        self.logger('It\'s {0}. Going to sleep for {1} seconds'.format(info, round(sleep, 1)))
                        self._sleep(time.time() + sleep + 5)
                    else:
                        self.logger('Could not load data: {0}'.format(data['status']))
                        sleep = Astro._seconds_to_tomorrow()
                        self._sleep(time.time() + sleep + 5)
                except Exception as ex:
                    self.logger('Error figuring out where the sun is: {0}'.format(ex))
                    sleep = Astro._seconds_to_tomorrow()
                    self._sleep(time.time() + sleep + 5)
            else:
                self._sleep(time.time() + 30)

    @om_expose
    def get_config_description(self):
        return json.dumps(Astro.config_description)

    @om_expose
    def get_config(self):
        return json.dumps(self._config)

    @om_expose
    def set_config(self, config):
        config = json.loads(config)
        for key in config:
            if isinstance(config[key], basestring):
                config[key] = str(config[key])
        self._config_checker.check_config(config)
        self._config = config
        self._read_config()
        self.write_config(config)
        return json.dumps({'success': True})

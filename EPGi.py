# -*- coding: utf-8 -*-

import configparser
import logging
import curses
import locale as locale_module
from datetime import datetime
import requests
import gzip
import xml.etree.ElementTree as ET

try:
    import pytz
except ImportError:
    print("Error: 'pytz' library is required. Please install it using 'pip install pytz'")
    exit(1)


class EPGProvider:
    """
    Handles fetching, parsing, and caching EPG data from a single provider URL.
    """
    def __init__(self, url):
        self.url = url
        self._channel_data = None  # In-memory cache

    def get_channels(self):
        """
        Returns a list of channels with their program data.
        Uses cached data if available.
        """
        if self._channel_data is not None:
            logging.info(f"Returning cached channel data for {self.url}")
            return self._channel_data

        logging.info(f"Fetching and parsing new data from {self.url}")
        try:
            response = requests.get(self.url, timeout=20)
            response.raise_for_status()

            content = response.content
            if self.url.endswith('.gz') or response.headers.get('Content-Type') == 'application/gzip':
                xml_data = gzip.decompress(content)
            else:
                xml_data = content

            self._channel_data = self._parse_xml(xml_data)
            logging.info(f"Successfully parsed {len(self._channel_data)} channels from {self.url}")
            return self._channel_data

        except requests.RequestException as e:
            logging.error(f"Failed to download EPG data from {self.url}: {e}")
        except gzip.BadGzipFile as e:
            logging.error(f"Failed to decompress Gzip file from {self.url}: {e}")
        except ET.ParseError as e:
            logging.error(f"Failed to parse XML from {self.url}: {e}")
        except Exception as e:
            logging.error(f"An unexpected error occurred while processing {self.url}: {e}", exc_info=True)

        self._channel_data = []  # Cache empty list on error to prevent retries
        return self._channel_data

    def _parse_xml(self, xml_data):
        """Parses the XMLTV data string into a structured list of channels."""
        root = ET.fromstring(xml_data)

        channels = {}
        for channel_elem in root.findall('channel'):
            channel_id = channel_elem.get('id')
            display_name_elem = channel_elem.find('display-name')
            if channel_id and display_name_elem is not None and display_name_elem.text:
                channels[channel_id] = {'name': display_name_elem.text, 'programmes': []}

        dt_format = "%Y%m%d%H%M%S %z"
        for prog_elem in root.findall('programme'):
            channel_id = prog_elem.get('channel')
            if channel_id in channels:
                try:
                    title_elem = prog_elem.find('title')
                    title = title_elem.text if title_elem is not None else "No Title"

                    start_time = datetime.strptime(prog_elem.get('start'), dt_format)
                    stop_time = datetime.strptime(prog_elem.get('stop'), dt_format)

                    # Store all program attributes for Screen 4
                    attributes = {**prog_elem.attrib}
                    for child in prog_elem:
                        if child.text and child.text.strip():
                            tag = child.tag
                            value = child.text.strip()
                            if tag not in attributes:
                                attributes[tag] = value
                            else:
                                if not isinstance(attributes[tag], list):
                                    attributes[tag] = [attributes[tag]]
                                attributes[tag].append(value)

                    programme_data = {
                        'title': title,
                        'start': start_time,
                        'stop': stop_time,
                        'attributes': attributes
                    }
                    channels[channel_id]['programmes'].append(programme_data)
                except (ValueError, TypeError) as e:
                    logging.warning(f"Skipping program due to parse error in {self.url}: {e}")

        # Sort programs within each channel by start time
        for channel in channels.values():
            channel['programmes'].sort(key=lambda p: p['start'])

        channel_list = list(channels.values())
        channel_list.sort(key=lambda c: c['name'])

        return channel_list


class Config:
    """
    Handles parsing and storing configuration from EPGi.ini.
    """
    def __init__(self, path='EPGi.ini'):
        self.config = configparser.ConfigParser(interpolation=None)
        if not self.config.read(path, encoding='utf-8'):
            raise FileNotFoundError(f"Configuration file not found: {path}")
        self.load_settings()

    def load_settings(self):
        """Loads all settings from the config file."""
        # Colors (as strings, to be processed by the UI module)
        self.col1_str = self.config.get('DEFAULT', 'col1', fallback='COLOR_WHITE,COLOR_BLACK')
        self.col2_str = self.config.get('DEFAULT', 'col2', fallback='COLOR_BLACK,COLOR_WHITE')
        self.col3_str = self.config.get('DEFAULT', 'col3', fallback='COLOR_RED,COLOR_BLACK')
        self.col4_str = self.config.get('DEFAULT', 'col4', fallback='COLOR_GREEN,COLOR_BLACK')

        # EPG URLs
        self.urls = []
        for i in range(1, 10):
            url = self.config.get('DEFAULT', f'url{i}', fallback=None)
            if url:
                self.urls.append(url)

        # Timezone
        self.tz_str = self.config.get('DEFAULT', 'tz', fallback=None)
        if self.tz_str:
            try:
                self.tz = pytz.timezone(self.tz_str)
            except pytz.UnknownTimeZoneError:
                logging.warning(f"Unknown timezone '{self.tz_str}'. Falling back to system default.")
                self.tz = datetime.now().astimezone().tzinfo
        else:
            self.tz = datetime.now().astimezone().tzinfo

        # Locale
        self.locale = self.config.get('DEFAULT', 'locale', fallback=None)

        # Date/Time formats
        self.date_fmt = self.config.get('DEFAULT', 'date_fmt', fallback='%d.%m')
        self.time_fmt = self.config.get('DEFAULT', 'time_fmt', fallback='%H:%M')


def setup_logging():
    """Sets up logging to EPGi.log, appending messages."""
    log_formatter = logging.Formatter('%(asctime)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    log_handler = logging.FileHandler('EPGi.log', mode='a', encoding='utf-8')
    log_handler.setFormatter(log_formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if logger.hasHandlers():
        logger.handlers.clear()

    logger.addHandler(log_handler)


class BaseScreen:
    """A base class for all screens in the application."""
    def __init__(self, stdscr, config, app):
        self.stdscr = stdscr
        self.config = config
        self.app = app # A reference back to the main EPGi app instance
        self.height, self.width = stdscr.getmaxyx()

    def display(self):
        """Draws the screen's content. Must be implemented by subclasses."""
        raise NotImplementedError

    def handle_input(self, key):
        """
        Processes user input. Must be implemented by subclasses.
        Should return an action, e.g., 'EXIT', 'BACK', or a new screen instance.
        """
        raise NotImplementedError


class Screen1(BaseScreen):
    """Screen 1: Displays the list of EPG providers."""
    def __init__(self, stdscr, config, app):
        super().__init__(stdscr, config, app)
        self.providers = self.config.urls
        self.current_line = 0
        self.top_line = 0

    def display(self):
        self.stdscr.clear()
        page_size = self.height

        for i in range(page_size):
            list_index = self.top_line + i
            if list_index >= len(self.providers):
                break

            provider_num = list_index + 1
            provider_url = self.providers[list_index]

            color = self.app.C_DEFAULT
            if list_index == self.current_line:
                color = self.app.C_CURRENT

            line_text = f"{provider_num:<2} {provider_url}"
            if len(line_text) > self.width:
                line_text = line_text[:self.width]

            self.stdscr.addstr(i, 0, line_text, color)

        self.stdscr.refresh()

    def handle_input(self, key):
        num_providers = len(self.providers)
        page_size = self.height - 2

        if key == curses.KEY_UP:
            self.current_line = max(0, self.current_line - 1)
        elif key == curses.KEY_DOWN:
            self.current_line = min(num_providers - 1, self.current_line + 1)
        elif key == curses.KEY_HOME:
            self.current_line = 0
        elif key == curses.KEY_END:
            self.current_line = num_providers - 1
        elif key == curses.KEY_PPAGE:
            self.current_line = max(0, self.current_line - page_size)
        elif key == curses.KEY_NPAGE:
            self.current_line = min(num_providers - 1, self.current_line + page_size)
        elif key == curses.KEY_RIGHT:
            provider_num = self.current_line + 1
            return Screen2(self.stdscr, self.config, self.app, provider_num)
        elif key in [curses.KEY_LEFT, 27]: # 27 is Esc
            return 'EXIT'

        # Adjust viewport
        if self.current_line < self.top_line:
            self.top_line = self.current_line
        elif self.current_line >= self.top_line + page_size:
            self.top_line = self.current_line - page_size + 1

        return None


class Screen2(BaseScreen):
    """Screen 2: Displays the list of channels and their current programs."""
    def __init__(self, stdscr, config, app, provider_num):
        super().__init__(stdscr, config, app)
        self.provider_num = provider_num
        self.provider = self.app.providers[provider_num]

        self.all_channels = []
        self.filtered_channels = []
        self.current_line = 0
        self.top_line = 0
        self.filter_text = ""

        self._load_data()

    def _load_data(self):
        """Fetches and processes channel data to find current programs."""
        raw_channels = self.provider.get_channels()
        now = datetime.now(self.config.tz)

        current_programs = []
        for channel in raw_channels:
            for prog in channel['programmes']:
                if prog['start'] <= now < prog['stop']:
                    current_programs.append({'channel': channel, 'program': prog})
                    break # Found current program for this channel

        self.all_channels = current_programs
        self.filtered_channels = self.all_channels

    def _apply_filter(self):
        """Filters the channel list based on self.filter_text."""
        if not self.filter_text:
            self.filtered_channels = self.all_channels
        else:
            self.filtered_channels = [
                item for item in self.all_channels
                if self.filter_text.lower() in item['channel']['name'].lower()
            ]
        self.current_line = 0
        self.top_line = 0

    def display(self):
        self.stdscr.clear()
        page_size = self.height - 2  # For status bar and a potential footer

        # --- Status Bar ---
        filter_str = f"[F]ilter: '{self.filter_text}'" if self.filter_text else "[F]ilter: None"
        counts_str = f"{len(self.filtered_channels)}/{len(self.all_channels)}"
        keys_str = "(Ent=э4, →=э3, Esc/←=назад, C=clr)"
        status_line1 = f"{filter_str}  {counts_str}"

        self.stdscr.addstr(0, 0, status_line1, self.app.C_STATUS)
        # Right-align the keys help text
        if len(status_line1) + len(keys_str) < self.width:
             self.stdscr.addstr(0, self.width - len(keys_str) -1, keys_str, self.app.C_STATUS)

        # --- Table Content ---
        for i in range(page_size):
            list_index = self.top_line + i
            if list_index >= len(self.filtered_channels):
                break

            item = self.filtered_channels[list_index]
            channel_name = item['channel']['name']
            program = item['program']

            # Calculate progress
            now = datetime.now(self.config.tz)
            duration = (program['stop'] - program['start']).total_seconds()
            elapsed = (now - program['start']).total_seconds()
            progress_percent = min(100, max(0, int((elapsed / duration) * 100))) if duration > 0 else 0

            progress_bar_width = 10
            filled_blocks = int(progress_bar_width * progress_percent / 100)
            progress_bar = '█' * filled_blocks + ' ' * (progress_bar_width - filled_blocks)

            color = self.app.C_DEFAULT
            if list_index == self.current_line:
                color = self.app.C_CURRENT

            # Column widths
            c1_w, c3_w, c4_w = 20, 4, 10
            sep_w = 3 # 3 spaces between columns
            c2_w = self.width - c1_w - c3_w - c4_w - sep_w

            line = f"{channel_name:<{c1_w}.{c1_w}} {program['title']:<{c2_w}.{c2_w}} {progress_percent:>3}% {progress_bar}"
            if len(line) > self.width:
                line = line[:self.width]

            self.stdscr.addstr(i + 1, 0, line, color)

        self.stdscr.refresh()

    def _get_user_input(self, prompt):
        self.stdscr.addstr(0, 0, " " * self.width, self.app.C_STATUS) # Clear status bar
        self.stdscr.addstr(0, 0, prompt, self.app.C_STATUS)
        curses.echo()
        curses.curs_set(1)
        input_str = self.stdscr.getstr(0, len(prompt)).decode('utf-8')
        curses.noecho()
        curses.curs_set(0)
        return input_str

    def handle_input(self, key):
        num_channels = len(self.filtered_channels)
        page_size = self.height - 2

        if key == curses.KEY_UP:
            self.current_line = max(0, self.current_line - 1)
        elif key == curses.KEY_DOWN:
            self.current_line = min(num_channels - 1, self.current_line + 1)
        elif key == curses.KEY_HOME:
            self.current_line = 0
        elif key == curses.KEY_END:
            self.current_line = num_channels - 1
        elif key == curses.KEY_PPAGE:
            self.current_line = max(0, self.current_line - page_size)
        elif key == curses.KEY_NPAGE:
            self.current_line = min(num_channels - 1, self.current_line + page_size)
        elif key in [ord('f'), ord('F')]:
            self.filter_text = self._get_user_input(f"[F]ilter: '{self.filter_text}' > ")
            self._apply_filter()
        elif key in [ord('c'), ord('C')]:
            self.filter_text = ""
            self._apply_filter()
        elif key == curses.KEY_RIGHT:
            return Screen3(self.stdscr, self.config, self.app, self.filtered_channels[self.current_line])
        elif key == curses.KEY_ENTER or key == 10:
            return Screen4(self.stdscr, self.config, self.app, self.filtered_channels[self.current_line])
        elif key in [curses.KEY_LEFT, 27]:
            return 'BACK'

        # Adjust viewport
        if self.current_line < self.top_line:
            self.top_line = self.current_line
        elif self.current_line >= self.top_line + page_size:
            self.top_line = self.current_line - page_size + 1

        return None

class Screen3(BaseScreen):
    """Screen 3: Displays the program guide for a single channel."""
    def __init__(self, stdscr, config, app, channel_item):
        super().__init__(stdscr, config, app)
        self.channel_data = channel_item['channel']
        self.programmes = self.channel_data['programmes']

        current_program = channel_item['program']
        try:
            self.current_line = self.programmes.index(current_program)
        except ValueError:
            self.current_line = 0

        # Set initial viewport to show previous, current, and next programs
        self.top_line = max(0, self.current_line - 1)

    def display(self):
        self.stdscr.clear()
        page_size = self.height
        now = datetime.now(self.config.tz)

        for i in range(page_size):
            list_index = self.top_line + i
            if list_index >= len(self.programmes):
                break

            program = self.programmes[list_index]
            start_time = program['start'].astimezone(self.config.tz)
            stop_time = program['stop'].astimezone(self.config.tz)

            if stop_time < now:
                color = self.app.C_PAST
            elif list_index == self.current_line:
                color = self.app.C_CURRENT
            else:
                color = self.app.C_DEFAULT

            date_str = start_time.strftime(self.config.date_fmt)
            time_str = start_time.strftime(self.config.time_fmt)
            title_str = program['title']

            c1_w, c2_w = 5, 5 # dd.mm, HH:MM
            sep = " "
            line = f"{date_str:<{c1_w}}{sep}{time_str:<{c2_w}}{sep}{title_str}"
            if len(line) > self.width:
                line = line[:self.width]

            self.stdscr.addstr(i, 0, line, color)

        self.stdscr.refresh()

    def handle_input(self, key):
        num_programmes = len(self.programmes)
        page_size = self.height - 2

        if key == curses.KEY_UP:
            self.current_line = max(0, self.current_line - 1)
        elif key == curses.KEY_DOWN:
            self.current_line = min(num_programmes - 1, self.current_line + 1)
        elif key == curses.KEY_HOME:
            self.current_line = 0
        elif key == curses.KEY_END:
            self.current_line = num_programmes - 1
        elif key == curses.KEY_PPAGE:
            self.current_line = max(0, self.current_line - page_size)
        elif key == curses.KEY_NPAGE:
            self.current_line = min(num_programmes - 1, self.current_line + page_size)
        elif key in [curses.KEY_RIGHT, curses.KEY_ENTER, 10]:
            selected_program = self.programmes[self.current_line]
            new_item = {'channel': self.channel_data, 'program': selected_program}
            return Screen4(self.stdscr, self.config, self.app, new_item)
        elif key in [curses.KEY_LEFT, 27]:
            return 'BACK'

        # Adjust viewport
        if self.current_line < self.top_line:
            self.top_line = self.current_line
        elif self.current_line >= self.top_line + page_size:
            self.top_line = self.current_line - page_size + 1

        return None

import textwrap

class Screen4(BaseScreen):
    """Screen 4: Displays all attributes for a program."""
    def __init__(self, stdscr, config, app, channel_item):
        super().__init__(stdscr, config, app)
        self.program = channel_item['program']
        self.attributes = self.program.get('attributes', {})
        self.lines = []
        self.top_line = 0
        self._prepare_lines()

    def _prepare_lines(self):
        """Converts the attributes dict into a list of wrapped lines for display."""
        self.lines = []
        if not self.attributes:
            self.lines.append("No attributes found for this program.")
            return

        wrap_width = self.width - 2 if self.width > 4 else self.width

        for key, value in sorted(self.attributes.items()):
            if isinstance(value, list):
                value_str = ", ".join(value)
            else:
                value_str = str(value)

            first_line_prefix = f"{key}: "
            subsequent_indent = " " * (len(key) + 2)

            wrapper = textwrap.TextWrapper(
                width=wrap_width,
                initial_indent=first_line_prefix,
                subsequent_indent=subsequent_indent,
                break_long_words=True,
                break_on_hyphens=True
            )

            wrapped_lines = wrapper.wrap(value_str)
            self.lines.extend(wrapped_lines)
            self.lines.append("") # Blank line for readability

    def display(self):
        self.stdscr.clear()
        page_size = self.height

        for i in range(page_size):
            line_idx = self.top_line + i
            if line_idx >= len(self.lines):
                break

            self.stdscr.addstr(i, 1, self.lines[line_idx], self.app.C_DEFAULT)

        self.stdscr.refresh()

    def handle_input(self, key):
        num_lines = len(self.lines)
        page_size = self.height - 2

        if key == curses.KEY_UP:
            self.top_line = max(0, self.top_line - 1)
        elif key == curses.KEY_DOWN:
            if num_lines > page_size:
                self.top_line = min(num_lines - page_size, self.top_line + 1)
        elif key == curses.KEY_PPAGE:
            self.top_line = max(0, self.top_line - page_size)
        elif key == curses.KEY_NPAGE:
            if num_lines > page_size:
                self.top_line = min(num_lines - page_size, self.top_line + page_size)
        elif key in [curses.KEY_LEFT, 27]:
            return 'BACK'

        return None


class EPGi:
    """Main application class."""
    def __init__(self, stdscr, config):
        self.stdscr = stdscr
        self.config = config
        self.providers = {i: EPGProvider(url) for i, url in enumerate(config.urls, 1)}

        self._init_curses()

        # Start with Screen 1 on the stack
        self.screen_stack = [Screen1(self.stdscr, self.config, self)]

    def _init_curses(self):
        """Initializes curses settings and color pairs."""
        curses.curs_set(0)
        curses.noecho()
        self.stdscr.keypad(True)

        curses.start_color()
        curses.use_default_colors()

        self.C_DEFAULT = self._create_color_pair(1, self.config.col1_str)
        self.C_CURRENT = self._create_color_pair(2, self.config.col2_str)
        self.C_PAST = self._create_color_pair(3, self.config.col3_str)
        self.C_STATUS = self._create_color_pair(4, self.config.col4_str)

    def _create_color_pair(self, pair_number, color_string):
        """Helper to create a color pair from a 'COLOR_FG,COLOR_BG' string."""
        fg_str, bg_str = color_string.split(',')
        fg = getattr(curses, fg_str.strip(), curses.COLOR_WHITE)
        bg = getattr(curses, bg_str.strip(), curses.COLOR_BLACK)
        curses.init_pair(pair_number, fg, bg)
        return curses.color_pair(pair_number)

    def run(self):
        """Main application loop with screen stack."""
        while self.screen_stack:
            current_screen = self.screen_stack[-1]
            current_screen.display()

            key = self.stdscr.getch()

            action = current_screen.handle_input(key)

            if action == 'EXIT':
                break
            elif action == 'BACK':
                if len(self.screen_stack) > 1:
                    self.screen_stack.pop()
            elif isinstance(action, BaseScreen):
                self.screen_stack.append(action)


def main(stdscr):
    """Wrapped by curses to safely handle the screen."""
    try:
        config = Config()

        if config.locale:
            try:
                locale_module.setlocale(locale_module.LC_TIME, config.locale)
            except locale_module.Error as e:
                logging.warning(f"Could not set locale to '{config.locale}': {e}")

        app = EPGi(stdscr, config)
        app.run()

    except FileNotFoundError as e:
        # This will be caught outside the curses wrapper
        raise e
    except Exception as e:
        logging.error("An error occurred within the curses application.", exc_info=True)
        # We can't print here as curses has taken over the screen.
        # The error will be in the log.


if __name__ == '__main__':
    import sys

    setup_logging()

    # A simple CLI flag to test config loading without starting curses
    if '--test-config' in sys.argv:
        logging.info("EPGi program started in config test mode.")
        try:
            config = Config()
            print("Configuration loaded successfully.")
            print(f"URLs: {config.urls}")
            print(f"Timezone: {config.tz}")
            logging.info("Configuration parsed successfully.")
        except FileNotFoundError as e:
            logging.error(f"Configuration file error: {e}", exc_info=True)
            print(f"Error: {e}")
        except Exception as e:
            logging.error(f"An unexpected error occurred during config test: {e}", exc_info=True)
            print(f"An unexpected error occurred. Check EPGi.log for details.")
        finally:
            logging.info("EPGi program finished config test mode.")

    elif '--test-fetch' in sys.argv:
        logging.info("EPGi program started in fetch test mode.")
        try:
            config = Config()
            if not config.urls:
                print("No URLs configured in EPGi.ini.")
                logging.warning("Fetch test skipped: No URLs in config.")
            else:
                url_to_test = config.urls[0]
                print(f"Testing fetch from first URL: {url_to_test}")
                provider = EPGProvider(url_to_test)

                print("\n--- First call to get_channels() ---")
                channels = provider.get_channels()

                if channels:
                    print(f"Successfully fetched and parsed. Found {len(channels)} channels.")
                    first_channel = channels[0]
                    print(f"  Sample channel: {first_channel['name']}")
                    if first_channel['programmes']:
                        first_prog = first_channel['programmes'][0]
                        print(f"    Sample program: '{first_prog['title']}' starting at {first_prog['start']}")
                else:
                    print("Fetch test completed, but no channels were parsed. Check EPGi.log for errors.")

                print("\n--- Second call to get_channels() ---")
                channels_cached = provider.get_channels()
                if channels_cached is not None:
                    print(f"Successfully retrieved {len(channels_cached)} channels from cache.")
                else:
                    print("Cache test failed.")

        except Exception as e:
            logging.error(f"An unexpected error occurred during fetch test: {e}", exc_info=True)
            print(f"An unexpected error occurred. Check EPGi.log for details.")
        finally:
            logging.info("EPGi program finished fetch test mode.")

    else:
        # Normal execution with curses UI
        logging.info("EPGi program started.")
        try:
            curses.wrapper(main)
        except FileNotFoundError as e:
            logging.error(f"Configuration file error: {e}")
            print(f"Error: {e}. Please make sure EPGi.ini exists and the program is run from the correct directory.")
        except curses.error as e:
            logging.error(f"Curses initialization failed: {e}", exc_info=True)
            print("Error: Failed to initialize the user interface.")
            print("This program requires a terminal that supports curses, which may not be available in this environment.")
        except Exception as e:
            logging.error(f"An unhandled exception occurred: {e}", exc_info=True)
            print(f"An unexpected error occurred. Check EPGi.log for details.")
        finally:
            logging.info("EPGi program finished.")

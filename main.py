#YABOSS

import asyncio
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

import aiohttp
import toml
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup, Update)
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

RESTAURANT_DATA_FILENAME = "restaurant_monitor.toml"

@dataclass
class MenuItem:
    title: str
    venue_status: Optional[str] = None
    venue_id: Optional[str] = None


class Constants:
    # Default coordinates for Israel
    DEFAULT_LAT = 32.0852999
    DEFAULT_LON = 34.78176759999999
    # Default location name
    DEFAULT_LOCATION_NAME = f"{DEFAULT_LAT}, {DEFAULT_LON}"
    # Monitoring interval in seconds
    MONITORING_INTERVAL = 30
    # Maximum time to keep monitors (12 hours)
    MAX_MONITOR_TIME = timedelta(hours=12)
    # Cleanup interval (1 hour)
    CLEANUP_INTERVAL = 3600
    # Telegram message length limit
    MAX_MESSAGE_LENGTH = 4000
    # API endpoints
    GEOCODING_URL = "https://nominatim.openstreetmap.org/search"
    REVERSE_GEOCODING_URL = "https://nominatim.openstreetmap.org/reverse"
    WOLT_API_URL = "https://restaurant-api.wolt.com/v1/pages/search"
    # User agent for API requests
    USER_AGENT = "Firefox/102.0"


class RestaurantMonitor:
    def __init__(self):
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}
        self.user_monitors: Dict[int, Set[str]] = {}
        self.monitoring_users: Dict[str, Set[int]] = {}
        self.venue_coordinates: Dict[str, tuple] = {}
        self.monitoring_interval = Constants.MONITORING_INTERVAL
        self.bot_instance = None  # Will be set when bot starts
        self.user_locations: Dict[int, tuple] = {}  # user_id -> (lat, lon)
        # user_id -> location_name
        self.user_location_names: Dict[int, str] = {}

        # Initialize TOML data file
        self.data_file = RESTAURANT_DATA_FILENAME
        # Load existing data from TOML
        self.load_from_toml()
        # Start cleanup thread
        self.cleanup_thread = threading.Thread(
            target=self.cleanup_old_monitors, daemon=True)
        self.cleanup_thread.start()

    def save_to_toml(self):
        """Save monitoring data to TOML file"""
        # Prepare data to save
        monitors_data = {}
        for venue_identifier, users in self.monitoring_users.items():
            for user_id in users:
                # Extract venue_title and restaurant_name from venue_identifier
                if '_' in venue_identifier:
                    parts = venue_identifier.split('_', 1)
                    if len(parts) == 2:
                        venue_title, restaurant_name = parts
                        # Get coordinates
                        lat, lon = self.venue_coordinates.get(
                            venue_identifier, (0.0, 0.0))
                        # Create a unique key for this user-venue combination
                        key = f"{user_id}_{venue_title}_{restaurant_name}"
                        monitors_data[key] = {
                            'user_id': user_id,
                            'venue_title': venue_title,
                            'restaurant_name': restaurant_name,
                            'lat': lat,
                            'lon': lon,
                            'created_at': datetime.now().isoformat()
                        }

        # Prepare user locations data
        user_locations_data = {}
        for user_id, (lat, lon) in self.user_locations.items():
            user_locations_data[str(user_id)] = {
                'lat': lat,
                'lon': lon,
                'location_name': self.user_location_names.get(user_id, f"{lat}, {lon}")
            }

        # Save to TOML file
        data_to_save = {
            'monitors': monitors_data,
            'user_locations': user_locations_data
        }

        with open(self.data_file, 'w', encoding='utf-8') as f:  # Specify UTF-8 encoding
            toml.dump(data_to_save, f)

    def load_from_toml(self):
        """Load monitoring data from TOML file"""
        if not os.path.exists(self.data_file):
            return

        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:  # Specify UTF-8 encoding
                data = toml.load(f)

            # Load monitors
            monitors_data = data.get('monitors', {})

            for key, monitor_info in monitors_data.items():
                user_id = monitor_info['user_id']
                venue_title = monitor_info['venue_title']
                restaurant_name = monitor_info['restaurant_name']
                lat = monitor_info['lat']
                lon = monitor_info['lon']
                created_at_str = monitor_info['created_at']

                # Parse the datetime
                created_at = datetime.fromisoformat(created_at_str)

                # Check if monitor is older than 12 hours
                if datetime.now() - created_at > Constants.MAX_MONITOR_TIME:
                    continue  # Skip old monitors

                venue_identifier = f"{venue_title}_{restaurant_name}"

                # Add to in-memory tracking
                if venue_identifier not in self.monitoring_users:
                    self.monitoring_users[venue_identifier] = set()
                    # Store coordinates for later when bot is ready
                    self.venue_coordinates[venue_identifier] = (lat, lon)

                self.monitoring_users[venue_identifier].add(user_id)

                if user_id not in self.user_monitors:
                    self.user_monitors[user_id] = set()
                self.user_monitors[user_id].add(venue_identifier)

            # Load user locations
            user_locations_data = data.get('user_locations', {})
            for user_id_str, location_info in user_locations_data.items():
                user_id = int(user_id_str)
                lat = location_info['lat']
                lon = location_info['lon']
                location_name = location_info.get(
                    'location_name', f"{lat}, {lon}")
                self.user_locations[user_id] = (lat, lon)
                self.user_location_names[user_id] = location_name

        except Exception as e:
            print(f"Error loading from TOML: {e}")

    def get_user_location(self, user_id: int) -> tuple:
        """Get user's location, default if not set"""
        if user_id in self.user_locations:
            return self.user_locations[user_id]
        return (Constants.DEFAULT_LAT, Constants.DEFAULT_LON)  # Default location

    def get_user_location_name(self, user_id: int) -> str:
        """Get user's location name, default if not set"""
        if user_id in self.user_location_names:
            return self.user_location_names[user_id]
        lat, lon = self.get_user_location(user_id)
        return f"{lat}, {lon}"

    def set_user_location(self, user_id: int, lat: float, lon: float, location_name: str = None):
        """Set user's location"""
        self.user_locations[user_id] = (lat, lon)
        if location_name:
            # Sanitize location name to remove problematic characters
            sanitized_name = location_name.replace(
                '\x00', '').replace('\x01', '')
            self.user_location_names[user_id] = sanitized_name
        else:
            # If no name provided, use coordinates as fallback
            self.user_location_names[user_id] = f"{lat}, {lon}"
        # Save to TOML
        self.save_to_toml()

    def cleanup_old_monitors(self):
        """Periodically cleanup monitors older than 12 hours"""
        while True:
            try:
                # Sleep for 1 hour before next cleanup
                asyncio.run(asyncio.sleep(Constants.CLEANUP_INTERVAL))

                # Calculate cutoff time (12 hours ago)
                cutoff_time = datetime.now() - Constants.MAX_MONITOR_TIME

                # Reload data and filter out old entries
                if os.path.exists(self.data_file):
                    with open(self.data_file, 'r', encoding='utf-8') as f:  # Specify UTF-8 encoding
                        data = toml.load(f)

                    monitors_data = data.get('monitors', {})
                    filtered_monitors = {}

                    for key, monitor_info in monitors_data.items():
                        created_at_str = monitor_info['created_at']
                        created_at = datetime.fromisoformat(created_at_str)

                        if datetime.now() - created_at <= Constants.MAX_MONITOR_TIME:
                            filtered_monitors[key] = monitor_info
                        else:
                            # Remove from in-memory tracking if it exists
                            venue_title = monitor_info['venue_title']
                            restaurant_name = monitor_info['restaurant_name']
                            user_id = monitor_info['user_id']
                            venue_identifier = f"{venue_title}_{restaurant_name}"

                            # Remove from in-memory tracking
                            if venue_identifier in self.monitoring_users:
                                self.monitoring_users[venue_identifier].discard(
                                    user_id)

                                if len(self.monitoring_users[venue_identifier]) == 0:
                                    if venue_identifier in self.monitoring_tasks:
                                        self.monitoring_tasks[venue_identifier].cancel(
                                        )
                                        del self.monitoring_tasks[venue_identifier]
                                    if venue_identifier in self.venue_coordinates:
                                        del self.venue_coordinates[venue_identifier]
                                    del self.monitoring_users[venue_identifier]

                            if user_id in self.user_monitors:
                                self.user_monitors[user_id].discard(
                                    venue_identifier)
                                if len(self.user_monitors[user_id]) == 0:
                                    del self.user_monitors[user_id]

                    # Save the filtered data back
                    data['monitors'] = filtered_monitors
                    with open(self.data_file, 'w', encoding='utf-8') as f:  # Specify UTF-8 encoding
                        toml.dump(data, f)

                # Count how many were removed
                removed_count = len(monitors_data) - len(filtered_monitors)
                if removed_count > 0:
                    print(f"Cleaned up {removed_count} old monitoring entries")

            except Exception as e:
                print(f"Error during cleanup: {e}")

    async def geocode_location(self, location_query: str) -> Optional[tuple]:
        """
        Geocode a location query (e.g., city name, address) to get lat/lon
        Using Nominatim API (OpenStreetMap)
        """
        headers = {
            'User-Agent': Constants.USER_AGENT  # Required by Nominatim
        }
        params = {
            'q': location_query,
            'format': 'json',
            'limit': 1
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(Constants.GEOCODING_URL, headers=headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data and len(data) > 0:
                            lat = float(data[0]['lat'])
                            lon = float(data[0]['lon'])
                            return (lat, lon)
                    else:
                        print(
                            f"Geocoding failed with status: {response.status}")
        except Exception as e:
            print(f"Error geocoding location: {e}")

        return None

    async def reverse_geocode_location(self, lat: float, lon: float) -> Optional[str]:
        """
        Reverse geocode coordinates to get location name
        Using Nominatim API (OpenStreetMap)
        """
        headers = {
            'User-Agent': Constants.USER_AGENT  # Required by Nominatim
        }
        params = {
            'lat': str(lat),
            'lon': str(lon),
            'format': 'json',
            'addressdetails': 1
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(Constants.REVERSE_GEOCODING_URL, headers=headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        if 'display_name' in data:
                            return data['display_name']
                    else:
                        print(
                            f"Reverse geocoding failed with status: {response.status}")
        except Exception as e:
            print(f"Error reverse geocoding location: {e}")

        return f"{lat}, {lon}"  # Fallback to coordinates

    async def make_post_request(self, query: str, lat: float, lon: float) -> Optional[dict]:
        headers = {
            "Content-Type": "application/json"
        }
        payload = {
            "q": query,
            "target": "venues",
            "lat": lat,
            "lon": lon
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(Constants.WOLT_API_URL, headers=headers, json=payload) as response:
                    if response.status == 200:
                        response_data = await response.json()
                        return response_data
                    else:
                        print(
                            f"Request failed with status code: {response.status}")
                        return None

        except aiohttp.ClientConnectionError as e:
            print(f"Connection error occurred: {e}")
        except aiohttp.ClientTimeout as e:
            print(f"Request timed out: {e}")
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON response: {e}")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")

        return None

    def extract_menu_items(self, response_data) -> List[MenuItem]:
        menu_items = []

        if not response_data or "sections" not in response_data:
            return menu_items

        for section in response_data["sections"]:
            if "items" not in section:
                continue

            for item in section["items"]:
                template = item.get("template", "")

                if template == "venue":
                    title = item.get("title", "")

                    venue_data = item.get("venue", {})
                    online = venue_data.get("online", None)

                    if "overlay" in item:
                        venue_status = item["overlay"]
                    elif online is False:
                        venue_status = "Temporarily offline"
                    else:
                        venue_status = "Online" if online else "Offline"

                    venue_identifier = f"{title}_{response_data.get('query', 'default')}"

                    menu_item = MenuItem(
                        title=title,
                        venue_status=venue_status,
                        venue_id=venue_identifier
                    )
                    menu_items.append(menu_item)

        return menu_items

    async def check_venue_status(self, venue_title: str, query: str, lat: float, lon: float) -> Optional[str]:
        """Check the status of a specific venue by searching again"""
        response_data = await self.make_post_request(query, lat, lon)

        if not response_data or "sections" not in response_data:
            return None

        venues = self.extract_menu_items(response_data)

        for venue in venues:
            if venue.title == venue_title:
                return venue.venue_status

        return None

    async def monitor_venue(self, venue_title: str, query: str, lat: float, lon: float, initial_status: str, user_id: int):
        """Monitor a venue and notify users when status changes"""
        current_status = initial_status
        venue_identifier = f"{venue_title}_{query}"

        print(f"Started monitoring {venue_title} for user {user_id}")

        while venue_identifier in self.monitoring_users and user_id in self.monitoring_users[venue_identifier]:
            await asyncio.sleep(self.monitoring_interval)

            new_status = await self.check_venue_status(venue_title, query, lat, lon)

            if new_status and new_status != current_status:
                print(
                    f"Status changed for {venue_title}: {current_status} -> {new_status}")

                if new_status == "Online" and current_status == "Temporarily offline":
                    users_to_notify = self.monitoring_users[venue_identifier].copy(
                    )
                    for uid in users_to_notify:
                        try:
                            await self.send_safe_message(uid, f"🎉 Good news! '{venue_title}' is now available for ordering!")
                            print(
                                f"Notified user {uid} about {venue_title} availability")
                        except Exception as e:
                            print(f"Error notifying user {uid}: {e}")

                current_status = new_status

    async def send_safe_message(self, chat_id: int, text: str):
        """Send message with proper error handling"""
        # Sanitize text to remove problematic characters
        safe_text = text.replace('\x00', '').replace('\x01', '')

        try:
            # Truncate if too long (Telegram limit is 4096 characters)
            if len(safe_text) > Constants.MAX_MESSAGE_LENGTH:
                safe_text = safe_text[:Constants.MAX_MESSAGE_LENGTH] + "..."

            # Remove any other problematic characters
            safe_text = safe_text.replace(
                '\x00', '').replace('\x01', '').strip()

            await self.bot_instance.send_message(chat_id=chat_id, text=safe_text)
        except BadRequest as e:
            print(f"Bad request when sending message to {chat_id}: {e}")
            # Try to send a simpler message
            simple_text = f"Good news! A restaurant is now available!"
            try:
                await self.bot_instance.send_message(chat_id=chat_id, text=simple_text[:Constants.MAX_MESSAGE_LENGTH])
            except Exception as e2:
                print(f"Failed to send simple message: {e2}")
        except Exception as e:
            print(f"Error sending message to {chat_id}: {e}")

    def start_monitoring(self, venue_title: str, query: str, lat: float, lon: float, initial_status: str, user_id: int):
        """Start monitoring a venue for a user"""
        venue_identifier = f"{venue_title}_{query}"

        if venue_identifier not in self.monitoring_users:
            self.monitoring_users[venue_identifier] = set()
            # Create the task only when we have the bot instance
            if self.bot_instance:
                task = asyncio.create_task(
                    self.monitor_venue(venue_title, query, lat,
                                       lon, initial_status, user_id)
                )
                self.monitoring_tasks[venue_identifier] = task
            self.venue_coordinates[venue_identifier] = (lat, lon)

        self.monitoring_users[venue_identifier].add(user_id)

        if user_id not in self.user_monitors:
            self.user_monitors[user_id] = set()
        self.user_monitors[user_id].add(venue_identifier)

        # Save to TOML
        self.save_to_toml()

    def stop_monitoring(self, venue_identifier: str, user_id: int):
        """Stop monitoring a venue for a user"""
        if venue_identifier in self.monitoring_users:
            self.monitoring_users[venue_identifier].discard(user_id)

            if len(self.monitoring_users[venue_identifier]) == 0:
                if venue_identifier in self.monitoring_tasks:
                    self.monitoring_tasks[venue_identifier].cancel()
                    del self.monitoring_tasks[venue_identifier]
                if venue_identifier in self.venue_coordinates:
                    del self.venue_coordinates[venue_identifier]
                del self.monitoring_users[venue_identifier]

        if user_id in self.user_monitors:
            self.user_monitors[user_id].discard(venue_identifier)
            if len(self.user_monitors[user_id]) == 0:
                del self.user_monitors[user_id]

        # Save to TOML
        self.save_to_toml()

    def is_already_monitoring(self, venue_identifier: str, user_id: int) -> bool:
        """Check if user is already monitoring this venue"""
        return (user_id in self.user_monitors and
                venue_identifier in self.user_monitors[user_id])

    def get_user_monitored_venues(self, user_id: int) -> List[str]:
        """Get list of venues a user is monitoring"""
        if user_id in self.user_monitors:
            return list(self.user_monitors[user_id])
        return []


restaurant_monitor = RestaurantMonitor()


def generate_callback_data(venue_title: str, restaurant_name: str, venue_status: str) -> str:
    """Generate a short callback data string using hash"""
    # Create a unique identifier
    identifier = f"{venue_title}_{restaurant_name}_{venue_status}"
    # Create a short hash
    hash_part = hashlib.md5(identifier.encode()).hexdigest()[:8]
    # Format: "select_hash"
    return f"select_{hash_part}"


def store_callback_data_mapping(callback_data: str, venue_title: str, restaurant_name: str, venue_status: str):
    """Store the mapping between callback data and actual values"""
    if not hasattr(store_callback_data_mapping, 'mapping'):
        store_callback_data_mapping.mapping = {}

    store_callback_data_mapping.mapping[callback_data] = {
        'venue_title': venue_title,
        'restaurant_name': restaurant_name,
        'venue_status': venue_status
    }


def get_callback_data_mapping(callback_data: str):
    """Retrieve the stored values for callback data"""
    if not hasattr(store_callback_data_mapping, 'mapping'):
        store_callback_data_mapping.mapping = {}

    return store_callback_data_mapping.mapping.get(callback_data)


def get_main_menu_keyboard():
    """Create main menu keyboard with command buttons"""
    keyboard = [
        [
            KeyboardButton("🔍 Search Restaurant"),
            KeyboardButton("📍 Change Location")
        ],
        [
            KeyboardButton("📋 My Monitored Venues"),
            KeyboardButton("❓ Help")
        ],
        [
            KeyboardButton("❌ Stop All Monitoring")
        ]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    try:
        welcome_message = (
            "🍽️ Welcome to Restaurant Monitor Bot!\n\n"
            "I'll help you monitor restaurant availability on Wolt.\n\n"
            "Use the buttons below or commands:\n"
            "/search - Search for a restaurant\n"
            "/location - Change your search location\n"
            "/monitored - View your monitored venues\n"
            "/help - Show help information\n"
            "/stopall - Stop monitoring all venues"
        )

        await update.message.reply_text(
            welcome_message,
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        print(f"Error in start_command: {e}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    try:
        help_text = (
            "❓ Restaurant Monitor Bot Help\n\n"
            "1. Send me a restaurant name to search\n"
            "2. Select from the results to monitor\n"
            "3. I'll notify you when it becomes available\n\n"
            "Available commands:\n"
            "/search - Search for a restaurant\n"
            "/location - Change your search location\n"
            "/monitored - View your monitored venues\n"
            "/help - Show this help\n"
            "/stopall - Stop monitoring all venues\n\n"
            "Use the buttons at the bottom of your screen for quick access!\n\n"
            "⚠️ Note: Monitored restaurants are automatically cleared after 12 hours."
        )

        await update.message.reply_text(help_text, reply_markup=get_main_menu_keyboard())
    except Exception as e:
        print(f"Error in help_command: {e}")


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /search command"""
    try:
        user_id = update.effective_user.id
        lat, lon = restaurant_monitor.get_user_location(user_id)
        location_name = restaurant_monitor.get_user_location_name(user_id)

        await update.message.reply_text(
            f"Current location: {location_name}\n\n"
            "Please send me the name of the restaurant you want to search for:",
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        print(f"Error in search_command: {e}")


async def monitored_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /monitored command"""
    try:
        user_id = update.effective_user.id
        monitored_venues = restaurant_monitor.get_user_monitored_venues(
            user_id)

        if not monitored_venues:
            await update.message.reply_text(
                "You're not monitoring any restaurants currently.\n\n"
                "Use the '🔍 Search Restaurant' button to start monitoring!",
                reply_markup=get_main_menu_keyboard()
            )
            return

        response_text = "📋 Your monitored restaurants:\n\n"
        for venue_identifier in monitored_venues:
            # Extract venue title from identifier
            venue_title = venue_identifier.split('_')[0]
            response_text += f"• {venue_title}\n"

        response_text += "\nI'll notify you when any of these become available!"

        await update.message.reply_text(response_text, reply_markup=get_main_menu_keyboard())
    except Exception as e:
        print(f"Error in monitored_command: {e}")


async def stopall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stopall command"""
    try:
        user_id = update.effective_user.id
        monitored_venues = restaurant_monitor.get_user_monitored_venues(
            user_id)

        if not monitored_venues:
            await update.message.reply_text(
                "You're not monitoring any restaurants to stop.",
                reply_markup=get_main_menu_keyboard()
            )
            return

        # Stop monitoring for all venues for this user
        venues_to_stop = monitored_venues.copy()
        for venue_identifier in venues_to_stop:
            restaurant_monitor.stop_monitoring(venue_identifier, user_id)

        await update.message.reply_text(
            f"✅ Successfully stopped monitoring for {len(venues_to_stop)} restaurant(s).",
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        print(f"Error in stopall_command: {e}")


async def location_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /location command"""
    try:
        user_id = update.effective_user.id
        lat, lon = restaurant_monitor.get_user_location(user_id)
        location_name = restaurant_monitor.get_user_location_name(user_id)

        await update.message.reply_text(
            f"Your current location is: {location_name}\n\n"
            "Please send me a location name (city, address, etc.) to update your search location:",
            reply_markup=get_main_menu_keyboard()
        )
        # Set flag to indicate user is updating location
        context.user_data['waiting_for_location'] = True
    except Exception as e:
        print(f"Error in location_command: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle restaurant name input and button commands"""
    try:
        user_id = update.effective_user.id
        message_text = update.message.text.strip()

        # Check if user is in the process of updating location
        if context.user_data.get('waiting_for_location'):
            await update.message.reply_text(f"Geocoding location: '{message_text}'...")

            # Geocode the location
            coords = await restaurant_monitor.geocode_location(message_text)

            if coords:
                lat, lon = coords
                # Get the display name for the location
                location_name = await restaurant_monitor.reverse_geocode_location(lat, lon)
                restaurant_monitor.set_user_location(
                    user_id, lat, lon, location_name)
                await update.message.reply_text(
                    f"✅ Location updated to: {location_name}\n\n"
                    "You can now search for restaurants in this area!",
                    reply_markup=get_main_menu_keyboard()
                )
            else:
                await update.message.reply_text(
                    "❌ Could not find that location. Please try again with a different location name.",
                    reply_markup=get_main_menu_keyboard()
                )

            # Clear the waiting flag
            context.user_data['waiting_for_location'] = False
            return

        # Handle button commands
        if message_text == "🔍 Search Restaurant":
            lat, lon = restaurant_monitor.get_user_location(user_id)
            location_name = restaurant_monitor.get_user_location_name(user_id)
            await update.message.reply_text(
                f"Current location: {location_name}\n\n"
                "Please send me the name of the restaurant you want to search for:",
                reply_markup=get_main_menu_keyboard()
            )
            return
        elif message_text == "📍 Change Location":
            lat, lon = restaurant_monitor.get_user_location(user_id)
            location_name = restaurant_monitor.get_user_location_name(user_id)
            await update.message.reply_text(
                f"Your current location is: {location_name}\n\n"
                "Please send me a location name (city, address, etc.) to update your search location:",
                reply_markup=get_main_menu_keyboard()
            )
            # Set flag to indicate user is updating location
            context.user_data['waiting_for_location'] = True
            return
        elif message_text == "📋 My Monitored Venues":
            monitored_venues = restaurant_monitor.get_user_monitored_venues(
                user_id)

            if not monitored_venues:
                await update.message.reply_text(
                    "You're not monitoring any restaurants currently.\n\n"
                    "Use the '🔍 Search Restaurant' button to start monitoring!",
                    reply_markup=get_main_menu_keyboard()
                )
                return

            response_text = "📋 Your monitored restaurants:\n\n"
            for venue_identifier in monitored_venues:
                venue_title = venue_identifier.split('_')[0]
                response_text += f"• {venue_title}\n"

            response_text += "\nI'll notify you when any of these become available!"

            await update.message.reply_text(response_text, reply_markup=get_main_menu_keyboard())
            return
        elif message_text == "❓ Help":
            help_text = (
                "❓ Restaurant Monitor Bot Help\n\n"
                "1. Send me a restaurant name to search\n"
                "2. Select from the results to monitor\n"
                "3. I'll notify you when it becomes available\n\n"
                "Available commands:\n"
                "/search - Search for a restaurant\n"
                "/location - Change your search location\n"
                "/monitored - View your monitored venues\n"
                "/help - Show this help\n"
                "/stopall - Stop monitoring all venues\n\n"
                "Use the buttons at the bottom of your screen for quick access!\n\n"
                "⚠️ Note: Monitored restaurants are automatically cleared after 12 hours."
            )

            await update.message.reply_text(help_text, reply_markup=get_main_menu_keyboard())
            return
        elif message_text == "❌ Stop All Monitoring":
            monitored_venues = restaurant_monitor.get_user_monitored_venues(
                user_id)

            if not monitored_venues:
                await update.message.reply_text(
                    "You're not monitoring any restaurants to stop.",
                    reply_markup=get_main_menu_keyboard()
                )
                return

            # Stop monitoring for all venues for this user
            venues_to_stop = monitored_venues.copy()
            for venue_identifier in venues_to_stop:
                restaurant_monitor.stop_monitoring(venue_identifier, user_id)

            await update.message.reply_text(
                f"✅ Successfully stopped monitoring for {len(venues_to_stop)} restaurant(s).",
                reply_markup=get_main_menu_keyboard()
            )
            return

        # Handle restaurant name input
        restaurant_name = message_text

        if not restaurant_name:
            await update.message.reply_text("Please send a restaurant name.")
            return

        # Get user's location
        lat, lon = restaurant_monitor.get_user_location(user_id)
        location_name = restaurant_monitor.get_user_location_name(user_id)

        await update.message.reply_text(f"Searching for '{restaurant_name}' in your location ({location_name})...")

        response_data = await restaurant_monitor.make_post_request(
            restaurant_name, lat, lon
        )

        if not response_data or "sections" not in response_data:
            await update.message.reply_text(
                "Sorry, I couldn't find any restaurants matching your query.",
                reply_markup=get_main_menu_keyboard()
            )
            return

        venues = restaurant_monitor.extract_menu_items(response_data)

        if not venues:
            await update.message.reply_text(
                f"No restaurants found for '{restaurant_name}'.",
                reply_markup=get_main_menu_keyboard()
            )
            return

        available_venues = [v for v in venues if v.venue_status == "Online"]
        if available_venues:
            response_text = "The following venues are already available:\n\n"
            for venue in available_venues:
                # Sanitize venue title
                safe_title = venue.title.replace(
                    '\x00', '').replace('\x01', '')
                response_text += f"✅ {safe_title}\n"
            response_text += "\nNo need to wait - they're ready to order from!"
            await update.message.reply_text(response_text, reply_markup=get_main_menu_keyboard())
            return

        keyboard = []
        for venue in venues:
            # Sanitize button text
            safe_title = venue.title.replace('\x00', '').replace('\x01', '')
            button_text = f"{safe_title} - {venue.venue_status}"

            # Generate short callback data
            callback_data = generate_callback_data(
                safe_title, restaurant_name, venue.venue_status)
            # Store the mapping
            store_callback_data_mapping(
                callback_data, safe_title, restaurant_name, venue.venue_status)

            keyboard.append([InlineKeyboardButton(
                button_text, callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Select a restaurant to monitor:",
            reply_markup=reply_markup
        )
    except Exception as e:
        print(f"Error in handle_message: {e}")
        await update.message.reply_text(
            "An error occurred while processing your request. Please try again.",
            reply_markup=get_main_menu_keyboard()
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button clicks"""
    try:
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id

        if query.data.startswith("select_"):
            # Retrieve the stored data
            stored_data = get_callback_data_mapping(query.data)
            if not stored_data:
                await query.edit_message_text("Error: Could not find the selected restaurant.")
                return

            venue_title = stored_data['venue_title']
            restaurant_name = stored_data['restaurant_name']
            venue_status = stored_data['venue_status']

            # Get user's location
            lat, lon = restaurant_monitor.get_user_location(user_id)

            venue_identifier = f"{venue_title}_{restaurant_name}"

            if restaurant_monitor.is_already_monitoring(venue_identifier, user_id):
                await query.edit_message_text(
                    f"You're already monitoring '{venue_title}'.\n"
                    "I'll keep checking for updates!"
                )
                return

            restaurant_monitor.start_monitoring(
                venue_title, restaurant_name, lat, lon, venue_status, user_id
            )

            await query.edit_message_text(
                f"Now monitoring '{venue_title}'.\n"
                f"Current status: {venue_status}\n\n"
                "I'll notify you when it becomes available!"
            )

            if venue_status == "Online":
                await restaurant_monitor.send_safe_message(
                    user_id, f"Good news! '{venue_title}' is already available for ordering!"
                )
    except Exception as e:
        print(f"Error in button_callback: {e}")


def main():
    print(list(os.environ))
    token = os.environ.get('BOT_TOKEN')
    if token is None:
        print("Error: BOT_TOKEN environment variable not set.")
        return
    
    application = Application.builder().token(os.environ.get('BOT_TOKEN')).build()

    # Set the bot instance for the monitor
    restaurant_monitor.bot_instance = application.bot

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("location", location_command))
    application.add_handler(CommandHandler("monitored", monitored_command))
    application.add_handler(CommandHandler("stopall", stopall_command))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))

    print("Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
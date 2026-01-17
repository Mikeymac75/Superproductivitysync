import os
import json
import time
import requests
import caldav
from caldav.elements import dav
from datetime import datetime, date, timedelta, timezone
import logging
from icalendar import Event, vDate, vDatetime
import sys

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Suppress verbose caldav library logging (it dumps full HTML pages)
logging.getLogger('caldav').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# Environment variables
WEBDAV_URL = os.environ.get('WEBDAV_URL')
WEBDAV_USERNAME = os.environ.get('WEBDAV_USERNAME')
WEBDAV_PASSWORD = os.environ.get('WEBDAV_PASSWORD')
CALDAV_URL = os.environ.get('CALDAV_URL')
CALDAV_USERNAME = os.environ.get('CALDAV_USERNAME')
CALDAV_PASSWORD = os.environ.get('CALDAV_PASSWORD')
CALENDAR_NAME = os.environ.get('CALENDAR_NAME')

def fetch_task_data():
    """Fetches the task data from WebDAV (supports single file, directory sync, or meta file)."""
    # Clean up the URL to avoid double slashes if user included trailing slash
    base_url = WEBDAV_URL.rstrip('/')
    
    # List of URLs to try. 
    # 1. The exact URL provided (e.g. pointing to _meta_ or backup.json)
    # 2. The URL + /task (for split file structure)
    # 3. The URL + /task.json
    urls_to_try = [
        WEBDAV_URL,
        f"{base_url}/task",
        f"{base_url}/task.json"
    ]

    for url in urls_to_try:
        try:
            logger.info(f"Attempting to fetch data from {url}")
            response = requests.get(url, auth=(WEBDAV_USERNAME, WEBDAV_PASSWORD), timeout=30)

            if response.status_code == 200:
                text_content = response.text
                
                # SPECIAL HANDLING: Handle Super Productivity 'pf_' prefix
                # The _meta_ file often starts with "pf_4.4__{...}" which is not valid JSON.
                if text_content.startswith('pf_'):
                    try:
                        # Split on the first '__' and take the rest
                        parts = text_content.split('__', 1)
                        if len(parts) > 1:
                            text_content = parts[1]
                            logger.info("Detected 'pf_' prefix. Stripped it for parsing.")
                    except Exception:
                        pass # If split fails, try parsing raw
                
                try:
                    data = json.loads(text_content)

                    # Case 1: Standard backup.json
                    if 'task' in data and 'entities' in data['task']:
                        logger.info("Detected standard backup.json format.")
                        return data

                    # Case 2: Individual task file (sync/task)
                    if 'entities' in data:
                        logger.info("Detected individual task file format.")
                        return {'task': data}
                        
                    # Case 3: Meta File (_meta_)
                    # This file bundles everything under 'mainModelData'
                    if 'mainModelData' in data and 'task' in data['mainModelData']:
                        logger.info("Detected _meta_ file format.")
                        return {'task': data['mainModelData']['task']}

                    # If we reached here, JSON is valid but keys are missing
                    logger.warning(f"Valid JSON found at {url} but missing expected keys. Keys found: {list(data.keys())}")

                except ValueError as e:
                    # Not JSON, likely a directory listing or other response
                    logger.warning(f"Failed to parse JSON from {url}: {e}. Content snippet: {text_content[:100]}")
                    continue
            else:
                logger.warning(f"HTTP {response.status_code} returned from {url}")

        except Exception as e:
            logger.warning(f"Failed to fetch from {url}: {e}")
            continue

    logger.error("Could not find valid task data at any attempted URL.")
    return None

def connect_caldav():
    """Connects to the CalDAV server and returns the calendar object.
    
    Uses DIRECT calendar URL access for better compatibility with TurnKey Nextcloud.
    Falls back to discovery mode if direct access fails.
    """
    try:
        logger.info(f"Connecting to CalDAV server...")
        logger.info(f"CALDAV_URL: {CALDAV_URL}")
        
        # Create client with the full calendar URL
        client = caldav.DAVClient(
            url=CALDAV_URL,
            username=CALDAV_USERNAME,
            password=CALDAV_PASSWORD
        )
        
        # Try direct calendar access first (works better with some Nextcloud setups)
        try:
            logger.info("Trying direct calendar access...")
            calendar = caldav.Calendar(client=client, url=CALDAV_URL)
            
            # Verify the calendar exists by trying to get its properties
            props = calendar.get_properties([dav.DisplayName()])
            display_name = props.get(dav.DisplayName(), 'Unknown')
            logger.info(f"Direct access successful! Calendar: '{display_name}'")
            logger.info(f"Calendar URL: {calendar.url}")
            return calendar
            
        except Exception as direct_err:
            logger.warning(f"Direct access failed: {direct_err}")
            logger.info("Falling back to discovery mode...")
        
        # Fallback: Use principal discovery
        if '/calendars/' in CALDAV_URL:
            base_url = CALDAV_URL.split('/calendars/')[0] + '/'
            calendar_path = '/calendars/' + CALDAV_URL.split('/calendars/')[1]
        else:
            base_url = CALDAV_URL
            calendar_path = None
        
        client = caldav.DAVClient(
            url=base_url,
            username=CALDAV_USERNAME,
            password=CALDAV_PASSWORD
        )
        
        principal = client.principal()
        calendars = principal.calendars()

        target_calendar = None
        logger.info(f"Found {len(calendars)} calendars via discovery.")
        
        for calendar in calendars:
            properties = calendar.get_properties([dav.DisplayName(), ])
            display_name = properties.get(dav.DisplayName(), '')
            cal_url = str(calendar.url) if calendar.url else ''
            
            logger.info(f"  - Calendar: '{display_name}' at {cal_url}")
            
            if calendar_path and calendar_path.rstrip('/') in cal_url:
                target_calendar = calendar
                logger.info(f"Matched by URL path: {display_name}")
                break
            elif display_name == CALENDAR_NAME or calendar.name == CALENDAR_NAME:
                target_calendar = calendar
                logger.info(f"Matched by name: {display_name}")
                break

        if not target_calendar:
            logger.error(f"Calendar not found. Tried matching path '{calendar_path}' or name '{CALENDAR_NAME}'")
            return None

        logger.info(f"Using calendar: {target_calendar.url}")
        return target_calendar
        
    except Exception as e:
        logger.error(f"Failed to connect to CalDAV: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def process_tasks(data, calendar):
    """Parses tasks and syncs them to the calendar."""
    if not data or 'task' not in data or 'entities' not in data['task']:
        logger.error("Invalid JSON structure: 'task.entities' not found.")
        return

    tasks = data['task']['entities']
    logger.info(f"Found {len(tasks)} tasks to check.")

    for task_id, task in tasks.items():
        # Skip subtasks - they have a parentId
        if task.get('parentId'):
            continue
            
        # Only sync tasks that have a due date
        due_day = task.get('dueDay')
        due_timestamp = task.get('dueWithTime')

        # Determine the due date/time
        dt_start = None
        is_all_day = False

        if due_timestamp:
            # Timestamp is usually milliseconds in JS/SuperProductivity
            # Python expects seconds.
            # Use UTC to ensure consistency
            dt_start = datetime.fromtimestamp(due_timestamp / 1000.0, tz=timezone.utc)
        elif due_day:
            try:
                dt_start = datetime.strptime(due_day, '%Y-%m-%d').date()
                is_all_day = True
            except ValueError:
                logger.warning(f"Invalid dueDay format for task {task_id}: {due_day}")
                continue
        
        # Skip tasks without due dates
        if not dt_start:
            continue

        title = task.get('title', 'Untitled Task')

        # Construct a unique ID for the calendar event
        # We prefix to avoid collisions if the user uses the calendar for other things
        uid = f"super-productivity-{task_id}"

        # Check if event exists
        try:
            existing_event = None
            try:
                existing_event = calendar.event_by_uid(uid)
            except caldav.error.NotFoundError:
                pass
            except Exception as e:
                # Some servers might throw other errors or if event_by_uid is not supported
                pass

            if existing_event:
                # Update the event if details changed.
                # Use icalendar component (best practice for caldav 2.0+)
                try:
                    # icalendar_component returns the VCALENDAR object which wraps VEVENT
                    ical = existing_event.icalendar_component
                except Exception:
                    logger.warning(f"Could not parse existing event for {title}, skipping update check.")
                    continue

                # We must find the VEVENT component inside the VCALENDAR
                vevent = None
                for component in ical.walk("VEVENT"):
                    vevent = component
                    break

                if not vevent:
                     logger.warning(f"No VEVENT found in existing event for {title}, skipping.")
                     continue

                # Check if we need to update
                # comp.get('dtstart').dt returns the native python object (date or datetime)
                current_start = vevent.get('dtstart').dt if vevent.get('dtstart') else None
                current_summary = str(vevent.get('summary')) if vevent.get('summary') else ""

                needs_update = False

                # Handling date vs datetime comparison
                # Ensure safe comparison between potentially naive and aware datetimes
                val_a = current_start.replace(tzinfo=None) if isinstance(current_start, datetime) else current_start
                val_b = dt_start.replace(tzinfo=None) if isinstance(dt_start, datetime) else dt_start

                if isinstance(current_start, datetime) and isinstance(dt_start, datetime):
                     if val_a != val_b:
                         needs_update = True
                elif isinstance(current_start, date) and isinstance(dt_start, date):
                    if val_a != val_b:
                        needs_update = True
                elif type(current_start) != type(dt_start):
                    # Type changed (e.g. all-day to time-based)
                    needs_update = True

                if title != current_summary:
                    needs_update = True

                if needs_update:
                    logger.info(f"Updating task: {title}")

                    # Update properties on the VEVENT component
                    vevent['summary'] = title

                    if is_all_day:
                        vevent['dtstart'] = vDate(dt_start)
                        vevent['dtend'] = vDate(dt_start + timedelta(days=1))
                        # Clean up duration if it exists
                        if 'DURATION' in vevent: del vevent['DURATION']
                    else:
                        vevent['dtstart'] = vDatetime(dt_start)
                        # Remove dtend/duration for point-in-time event, or keep if we wanted duration.
                        # For now, to be safe and avoid conflicts with old all-day data:
                        if 'DTEND' in vevent: del vevent['DTEND']
                        if 'DURATION' in vevent: del vevent['DURATION']

                    # Save the changes
                    # existing_event.icalendar_component is a property that returns the object derived from data
                    # To save, we usually need to convert back to ical and set data, or let library handle it.
                    # CalDAV library usually updates .data from .icalendar_component when save is called,
                    # provided we modified the SAME object instance.
                    existing_event.save()

            else:
                logger.info(f"Creating task: {title}")
                logger.info(f"  Calendar URL: {calendar.url}")
                # Use raw HTTP PUT instead of caldav library (more reliable for TurnKey Nextcloud)
                try:
                    # Check for recurrence configuration
                    repeat_cfg = task.get('repeatCfg')
                    rrule_line = ""
                    
                    # Debug: log the repeatCfg if present
                    if repeat_cfg:
                        logger.info(f"  repeatCfg found: {repeat_cfg} (type: {type(repeat_cfg).__name__})")
                    
                    # Also check for tagIds or other indicators of scheduled tasks
                    reminder_id = task.get('reminderId')
                    if reminder_id:
                        logger.info(f"  reminderId found: {reminder_id}")
                    
                    if repeat_cfg:
                        # Super Productivity repeatCfg can be:
                        # - A string like "DAILY", "WEEKLY", "MONTHLY", "YEARLY"
                        # - An object with more complex configuration
                        if isinstance(repeat_cfg, str):
                            freq_map = {
                                'DAILY': 'DAILY',
                                'WEEKLY': 'WEEKLY', 
                                'MONTHLY': 'MONTHLY',
                                'YEARLY': 'YEARLY'
                            }
                            freq = freq_map.get(repeat_cfg.upper())
                            if freq:
                                rrule_line = f"\nRRULE:FREQ={freq}"
                                logger.info(f"  Adding recurrence: {freq}")
                        elif isinstance(repeat_cfg, dict):
                            # Handle object-based config - log all keys to understand structure
                            logger.info(f"  repeatCfg keys: {list(repeat_cfg.keys())}")
                            freq = repeat_cfg.get('repeatEvery', repeat_cfg.get('frequency', 'DAILY'))
                            if isinstance(freq, str) and freq.upper() in ['DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY']:
                                rrule_line = f"\nRRULE:FREQ={freq.upper()}"
                                logger.info(f"  Adding recurrence: {freq.upper()}")
                    
                    # Build the iCal content manually
                    if is_all_day:
                        # Format date as YYYYMMDD for VALUE=DATE
                        dtstart_str = dt_start.strftime('%Y%m%d')
                        dtend = dt_start + timedelta(days=1)
                        dtend_str = dtend.strftime('%Y%m%d')
                        ical_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Super Productivity Sync//EN
BEGIN:VEVENT
UID:{uid}
DTSTART;VALUE=DATE:{dtstart_str}
DTEND;VALUE=DATE:{dtend_str}
SUMMARY:{title}{rrule_line}
END:VEVENT
END:VCALENDAR"""
                    else:
                        # Format datetime as YYYYMMDDTHHMMSSZ for UTC
                        dtstart_str = dt_start.strftime('%Y%m%dT%H%M%SZ')
                        ical_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Super Productivity Sync//EN
BEGIN:VEVENT
UID:{uid}
DTSTART:{dtstart_str}
SUMMARY:{title}{rrule_line}
END:VEVENT
END:VCALENDAR"""
                    
                    # Build the PUT URL
                    cal_url = str(calendar.url).rstrip('/')
                    event_url = f"{cal_url}/{uid}.ics"
                    
                    logger.info(f"  PUT URL: {event_url}")
                    
                    # Make raw HTTP PUT request
                    response = requests.put(
                        event_url,
                        data=ical_content,
                        auth=(CALDAV_USERNAME, CALDAV_PASSWORD),
                        headers={'Content-Type': 'text/calendar; charset=utf-8'},
                        timeout=30
                    )
                    
                    if response.status_code in [200, 201, 204]:
                        logger.info(f"  Successfully created event for: {title} (HTTP {response.status_code})")
                    else:
                        logger.error(f"  Failed to create event: HTTP {response.status_code}")
                        logger.error(f"  Response: {response.text[:500]}")
                except Exception as save_error:
                    logger.error(f"  Failed to save event: {save_error}")
                    # Log the actual URLs being used
                    logger.error(f"  Calendar URL was: {calendar.url}")
                    logger.error(f"  Calendar client URL: {calendar.client.url if calendar.client else 'N/A'}")
                    raise

        except Exception as e:
            logger.error(f"Error processing task {task_id}: {e}")

def main():
    logger.info("Starting Super Productivity Sync")

    # Run loop
    while True:
        try:
            json_data = fetch_task_data()
            if json_data:
                calendar = connect_caldav()
                if calendar:
                    process_tasks(json_data, calendar)

            logger.info("Sync complete. Sleeping for 15 minutes...")
        except Exception as e:
            logger.error(f"Global error: {e}")

        time.sleep(900) # 15 minutes

if __name__ == "__main__":
    # Check for required env vars
    required_vars = [
        'WEBDAV_URL', 'WEBDAV_USERNAME', 'WEBDAV_PASSWORD',
        'CALDAV_URL', 'CALDAV_USERNAME', 'CALDAV_PASSWORD',
        'CALENDAR_NAME'
    ]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        logger.error(f"Missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    main()

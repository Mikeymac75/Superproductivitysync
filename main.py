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

# Environment variables
WEBDAV_URL = os.environ.get('WEBDAV_URL')
WEBDAV_USERNAME = os.environ.get('WEBDAV_USERNAME')
WEBDAV_PASSWORD = os.environ.get('WEBDAV_PASSWORD')
CALDAV_URL = os.environ.get('CALDAV_URL')
CALDAV_USERNAME = os.environ.get('CALDAV_USERNAME')
CALDAV_PASSWORD = os.environ.get('CALDAV_PASSWORD')
CALENDAR_NAME = os.environ.get('CALENDAR_NAME')

def get_backup_json():
    """Fetches the backup.json from WebDAV."""
    try:
        logger.info(f"Fetching backup.json from {WEBDAV_URL}")
        # WebDAV often requires basic auth.
        response = requests.get(WEBDAV_URL, auth=(WEBDAV_USERNAME, WEBDAV_PASSWORD), timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch backup.json: {e}")
        return None

def connect_caldav():
    """Connects to the CalDAV server and returns the calendar object."""
    try:
        logger.info(f"Connecting to CalDAV server at {CALDAV_URL}")
        client = caldav.DAVClient(
            url=CALDAV_URL,
            username=CALDAV_USERNAME,
            password=CALDAV_PASSWORD
        )
        principal = client.principal()
        calendars = principal.calendars()

        target_calendar = None
        for calendar in calendars:
            # We check display name or name
            properties = calendar.get_properties([dav.DisplayName(), ])
            display_name = properties.get(dav.DisplayName(), '')
            if display_name == CALENDAR_NAME or calendar.name == CALENDAR_NAME:
                target_calendar = calendar
                break

        if not target_calendar:
            logger.error(f"Calendar '{CALENDAR_NAME}' not found.")
            return None

        return target_calendar
    except Exception as e:
        logger.error(f"Failed to connect to CalDAV: {e}")
        return None

def process_tasks(data, calendar):
    """Parses tasks and syncs them to the calendar."""
    if not data or 'task' not in data or 'entities' not in data['task']:
        logger.error("Invalid JSON structure: 'task.entities' not found.")
        return

    tasks = data['task']['entities']
    logger.info(f"Found {len(tasks)} tasks to check.")

    for task_id, task in tasks.items():
        # Only process tasks with a due date
        # dueDay is YYYY-MM-DD, dueWithTime is timestamp
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
                # Create new event
                # For all-day events, we usually need to specify dtend or duration?
                # caldav.save_event handles basics.
                if is_all_day:
                     calendar.save_event(
                        dtstart=dt_start,
                        dtend=dt_start + timedelta(days=1),
                        summary=title,
                        uid=uid
                    )
                else:
                    calendar.save_event(
                        dtstart=dt_start,
                        summary=title,
                        uid=uid
                    )

        except Exception as e:
            logger.error(f"Error processing task {task_id}: {e}")

def main():
    logger.info("Starting Super Productivity Sync")

    # Run loop
    while True:
        try:
            json_data = get_backup_json()
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

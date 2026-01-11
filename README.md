# Super Productivity to CalDAV Sync

A lightweight, standalone Docker container that automatically synchronizes your tasks from [Super Productivity](https://super-productivity.com/) to any CalDAV calendar (Nextcloud, Baikal, iCloud, etc.).

This is perfect for HomeLab users who want to see their "To Do" list on their family calendar or Home Assistant dashboard.

## 🚀 Features

*   **One-Way Sync**: Pushes tasks from Super Productivity -> CalDAV.
*   **Smart Updates**:
    *   Creates new events for tasks with a `Due Date`.
    *   Updates existing events if the Title or Due Date changes.
    *   Supports both **All Day** (Date only) and **Timed** (Date + Time) tasks.
*   **Privacy Focused**: Works entirely locally or with your self-hosted WebDAV/CalDAV server.
*   **Duplicate Prevention**: Uses unique UIDs to ensure tasks aren't duplicated if the script runs multiple times.

## 🛠️ Prerequisites

1.  **Super Productivity** configured to save backups to a **WebDAV** location (e.g., your Nextcloud instance).
2.  A **CalDAV Calendar** created specifically for these tasks (recommended: create a new calendar named `SuperProductivity` or `Tasks` in Nextcloud).

## 🐳 Quick Start (Docker Compose)

Add this service to your `docker-compose.yml`:

```yaml
version: '3.8'

services:
  super-productivity-sync:
    image: python:3.9-slim
    # In a real deployment, you would build the image or pull from a registry
    # build: .
    container_name: super-prod-sync
    restart: unless-stopped
    volumes:
      - .:/app  # If running from source, or just mount the script
    working_dir: /app
    command: python main.py
    environment:
      # --- WebDAV Settings (Where Super Productivity saves backup.json) ---
      - WEBDAV_URL=https://nextcloud.yourdomain.com/remote.php/dav/files/youruser/super-productivity/backup.json
      - WEBDAV_USERNAME=your_nextcloud_username
      - WEBDAV_PASSWORD=your_nextcloud_password

      # --- CalDAV Settings (Where events should be created) ---
      - CALDAV_URL=https://nextcloud.yourdomain.com/remote.php/dav/
      - CALDAV_USERNAME=your_nextcloud_username
      - CALDAV_PASSWORD=your_nextcloud_password
      - CALENDAR_NAME=Tasks  # Must match the display name in Nextcloud exactly
```

## ⚙️ Configuration Variables

| Variable | Description | Example |
| :--- | :--- | :--- |
| `WEBDAV_URL` | Full URL to your `backup.json` file. | `https://cloud.com/.../backup.json` |
| `WEBDAV_USERNAME` | Username for WebDAV access. | `admin` |
| `WEBDAV_PASSWORD` | Password (or App Password) for WebDAV. | `secret123` |
| `CALDAV_URL` | Base URL for your CalDAV server. | `https://cloud.com/remote.php/dav/` |
| `CALDAV_USERNAME` | Username for CalDAV access. | `admin` |
| `CALDAV_PASSWORD` | Password (or App Password) for CalDAV. | `secret123` |
| `CALENDAR_NAME` | Display name of the target calendar. | `Work Tasks` |

## 🧩 How it Works

1.  The script wakes up every **15 minutes**.
2.  It downloads the `backup.json` file from your specified WebDAV URL.
3.  It parses the active tasks to find any with a valid `dueDay` or `dueWithTime`.
4.  It connects to your CalDAV server and searches for events with a unique ID (`super-productivity-{task_id}`).
5.  If the event doesn't exist, it creates it.
6.  If it does exist, it checks if the Date or Title has changed and updates it if necessary.

## 🤝 Contributing

Feel free to fork this and add features (like two-way sync, although that's much harder!).

---
*Note: This is a community project and not officially affiliated with Super Productivity.*

# Use an official Python runtime as a parent image.
# NOTE: 3.11+ is required -- Archipelago 0.6.7 uses typing.Self (added in 3.11); the bot
# itself is fine on 3.10 but the analyzer venv built below would fail to import AP on it.
FROM python:3.12

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install the bot's dependencies (the main interpreter)
RUN pip install --no-cache-dir -r requirements.txt

# The go-mode analyzer runs as a short-lived subprocess in its OWN venv: Archipelago pins
# websockets==13.1 while the bot needs ~=15.0, so they cannot share one environment. The
# venv gets Archipelago's *logic* dependencies only (see gomode-ap-requirements.txt -- no
# GUI/Windows-only packages; a world that needs more simply reports "unsupported"). It lives
# in /opt so the `.:/app` bind-mount used at runtime can't shadow it. GOMODE_AP_PYTHON (set
# in docker-compose.yml) points the bot at this interpreter.
RUN python -m venv /opt/ap-venv \
 && /opt/ap-venv/bin/pip install --no-cache-dir -r gomode-ap-requirements.txt

# Command to run the application
CMD ["python", "main.py"]

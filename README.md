# APDiscordBot
This is a Python based Discord Bot for interacting with an Archipelago Multiworld server.

The bot will announce items received once a minute to a specific channel, can be used to get a list of all of the items received for a specific slot or a list of slots that you assign to yourself, and can even be used to track specific items for a slot to get a DM when you receive it.

## Setup

1. Clone the repo and change into the directory.

```
git clone https://github.com/AlexMKramer/APDiscordBot.git

cd APDiscordBot
```
2. Copy the .env_template to .env.

```
cp .env_template .env
```
3. Go to the Discord Developer portal and select New Application and name it whatever you'd like.  https://discord.com/developers/applications

4. Select OAuth2 on the left side of the screen, then under OAuth2 URL Generator, select bot, applications.commands, and Send Messages under bot permissions.

5. Edit the .env and fill out each field.  Use your preferred editor, I use nano.

   If you don't enter a Channel ID, the bot will only send items that you are tracking in a DM when requested or when tracked items are received.

```
nano .env
```

6. Save and exit.

### Docker Compose
I suggest running the app with Docker Compose, though it is not a requirement.

With docker already installed, build the container and run it.

```
docker compose up --build
```

Once it finishes building, it will start to run.  Once you see it say its logged in as the bot name you set in the Discord Developer portal, stop the container with ctrl+c.  

Run the container detatched.

```
docker compose up -d
```

## Discord
When using the bot with a server for the first time, you will need to have it connect to the AP server to gather item and slot info.

I recommend giving the bot its own "slot" for the server, though it is not a requirement.

You can see an example of another project's YAML that you would provide to the server admin before AP generation here: https://github.com/Quasky/bridgeipelago/blob/main/bridgeipleago.yaml

If you didn't set this up before generation, you can still use the bot, just provide it with a slot name that you have access to.

### Getting the Server Data

**You will only need to do this once.**


With the bot running, type "/get_server_data" followed by the server address (with the port), the slot name, and the password (if the slot has one), and hit enter.

```
/get_server_data archipelago.gg:38281 APBot password
```

It will then connect to the server using that slot name, send a message to the server, get the packets it needs, and disconnect.  

### Using Commands
You can see all of the commands for the bot in Discord by typing "/" and selecting the bot on the left.

Most commands are self explanatory, so I won't go over a majority of them.

To assign a slot to your username, type "/assign_slot" and select your slot name from the list.  If it fails to list any slot names, restart the bot and try again.

With slots assigned, you can run "/get_all_tracked_items" and it will send you all items you have received for the slots you have assigned yourself.

You can also run "/track_item" select the Game its for, the item name, an assigned slot, and the target amount you need.  You will then get a DM every time another one of those items is received, up to the amount you specified. 

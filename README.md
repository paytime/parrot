# Parrot
A discord bot for creating &amp; sharing tweets!

## Configuration
Parrot requires [Node.JS](https://nodejs.org/en/)! All packages can be installed with the command `npm install`!

To start Parrot, you need to first create a `config.json` file. The `config.example.json` can be used! After the `config.json` is fully set, run Parrot with: `npm start` !

### Properties
* **Token**: This is the token of the discord bot, where the app will connect to. See [this](https://discordapp.com/developers/applications/me)!
* **Prefix**: This is the prefix which can be used by ranked users to communicate with Parrot.
* **Allowed Roles**: This is an array of all roles who can access all commands.
* **Log**: You can toggle logging here. The logs will be saved in the `log.txt` file.
* **Twitter**: Register a Twitter App [here](https://apps.twitter.com) and insert all the keys in the fields. `Channels` is an array of all discord channel-id's, which will act as announcement channels!
* **Fun Channels**: Those are all channels where users with any role can use trivial commands.
* **Banned**: You can activate auto moderation here and set unmoderated channels as well as list all banned words.

## Commands & Features
Type `!help` in Discord to see all settings & commands. Parrot is capable of *tweeting* posts and sharing tweets to all set announcements channels!

Bot activity can be logged if turned on. The bot can also remove inappropriate messages if turned on.

## License
This project is licensed under the MIT License — see the [LICENSE](./LICENSE) file for details!

[© Paytime](https://github.com/paytime)

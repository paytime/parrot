const Discord = require("discord.js");
const bot = new Discord.Client();
const config = require("./config/config.json");
const dict = require("./dictionary.json");
const Twitter = require("twitter");
const fs = require('fs');
const dateFormat = require("dateformat");
const request = require('request').defaults({ encoding: null });

const blacklist = require("./config/blacklist.json");
const commands = require("./config/commands.json");

process.title = "parrot";

bot.on("ready", () => {
  logActivity(new Date(), "", "", "Parrot", dict.ready);
  bot.user.setGame("Type !help", "");
});

/**
 * How Parrot should respond to messages
 */
bot.on("message", (message) => {
  if (message.author.bot) return; // Prevents 'botception'
  if (message.member == null) return; // Ignores invalid messages

  var isAllowed = message.member.roles.some(r=>config.moderators.includes(r.name));

  if(config.blacklist.active && !isAllowed &&
    (config.blacklist.unmoderatedChannels.indexOf(message.channel.id) < 0) &&
    blacklist.some(word => message.content.includes(word))) { // User used blacklisted words in a moderated channel
      message.delete(100)
        .then(() => {
          logActivity(new Date(message.createdTimestamp), message.channel.name, message.guild.name, "Removed message from " + message.member.displayName, message.content); 
        })
        .catch((err) => {
          logActivity(new Date(), "", "", "Parrot", err);
        });
    return;
  }

  if (!message.content.startsWith(config.prefix)) return;

  const command = message.content.slice(config.prefix.length).trim().split(/ +/g).shift().toLowerCase();
  const args = message.content.slice(config.prefix.length + command.length).trim();

  if ((dict.commands.greet.indexOf(command) >= 0)) { // Parrot greets the user
    var randomNumber = Math.floor(Math.random()*dict.greetings.length);
    sendMessage(dict.greetings[randomNumber].replace("{user}", "<@" + message.author.id + ">").replace("{bot}", bot.user.username));
  } else if ((dict.commands.ask.indexOf(command) >= 0) && (args.length > 3))  { // Plays magic 8 ball
    var randomNumber = Math.floor(Math.random() * dict.magic8ball.length);
    sendMessage("<@" + message.author.id + "> " + dict.magic8ball[randomNumber]);
  } else if ((dict.commands.tweet.indexOf(command) >= 0) && isAllowed) {
    handleAnnouncement();
    logActivity(new Date(message.createdTimestamp), message.channel.name, message.guild.name, message.member.displayName, message.content);      
  } else if (dict.commands.help.indexOf(command) >= 0) {
    displayHelp();
  }

  /**
   * Displays all info & commands
   */
  function displayHelp() {
    if (args.isEmpty()) {
      sendMessage(
        {
          content: "<@" + message.author.id + "> | " + dict.copyright + " <https://github.com/paytime/parrot>",
          embed: {
            title: dict.settings.title,
            description: dict.settings.description,
            footer: {
              text: footer()
            },
            fields: [
              {
                name: dict.settings.banned,
                value: config.blacklist.active ? dict.settings.enabled : dict.settings.disabled,
                inline: true
              },
              {
                name: dict.settings.log,
                value: (config.log.fileLog || config.log.channels.length > 0) ? dict.settings.enabled : dict.settings.disabled,
                inline: true
              },
              {
                name: dict.settings.allowedRoles,
                value: config.moderators.toString()
              },
              {
                name: dict.settings.commands,
                value: dict.commands.help.toString() + "\r\n" + dict.commands.greet.toString() + "\r\n" + dict.commands.tweet.toString() + "\r\n" + dict.commands.ask.toString()
              }
            ]
          }
        }, true
      );
    } else {
      var mainArg = args.split(/ +/g).shift().toLowerCase();
      if (dict.commands.ask.indexOf(mainArg) >= 0) {
        sendMessage(dict.commandsHelp.ask);
      } else if (dict.commands.greet.indexOf(mainArg) >= 0) {
        sendMessage(dict.commandsHelp.greet);
      } else if (dict.commands.help.indexOf(mainArg) >= 0) {
        sendMessage(dict.commandsHelp.help);
      } else if (dict.commands.tweet.indexOf(mainArg) >= 0) {
        sendMessage(dict.commandsHelp.tweet);
      } else {
        sendMessage("<@" + message.author.id + "> " + dict.commandsHelp.helpNotFound);
      }
    }
  }

  /**
   * Handles TWEETS
   */
  function handleAnnouncement() {
    if(args.isEmpty()) {
      sendMessage(dict.tweet.emptyError);
      return;
    }

    var twitterClient = new Twitter({
      consumer_key: config.twitter.consumer_key,
      consumer_secret: config.twitter.consumer_secret,
      access_token_key: config.twitter.access_token_key,
      access_token_secret: config.twitter.access_token_secret
    });

    if (args.toLowerCase().startsWith(dict.tweet.getLatestTweet)) {
      getLatestTweet(twitterClient);
    } else if (args.toLowerCase().startsWith(dict.tweet.getTweet)) {
      var link = args.slice(dict.tweet.getTweet.length).trim().split(/ +/g).shift();
      if((link.length < 5) || (!link.includes("/"))) {
        sendMessage(dict.tweet.invalidLink + " " + link);
        return;
      }
      getTweet(twitterClient, link);
    } else {
      postAnnouncement(twitterClient);
    }
  }

  /**
   * Gets the content of a twitter link and shares it
   * @param {*} twitterClient 
   * @param {*} link 
   */
  function getTweet(twitterClient, link) {
    twitterClient.get('statuses/show', { 
      id : link.split("/").filter((i) => { return !i.isEmpty() }).pop() ,
      include_my_retweet : false,
      include_entities : true,
      include_ext_alt_text : true
    })
    .then((result) => {
      sendMessage(`${dict.tweet.sharingTweet}<https://twitter.com/${result.user.screen_name}/status/${result.id_str}>`);
      shareAnnouncement(result);
    })
    .catch((errors) => handleTweetErrors(errors));
  }

  /**
   * Gets the latest tweet from an account
   * @param {*} twitterClient 
   */
  function getLatestTweet(twitterClient) {
    twitterClient.get('statuses/user_timeline', { 
      user_id : config.twitter.access_token_key.split("-").shift(),
      exclude_replies : true 
    })
    .then((result) => {
      sendMessage(`${dict.tweet.sharingTweet}<https://twitter.com/${result[0].user.screen_name}/status/${result[0].id_str}>`);
      shareAnnouncement(result[0]);
    })
    .catch((errors) => handleTweetErrors(errors));
  }

  /**
   * Tweets a message
   * @param {*} twitterClient 
   */
  function postAnnouncement(twitterClient) {
    var formattedTweet = args.replace(" - ", " — ");

    if(message.attachments.array().length > 0) {
      request.get(message.attachments.array()[0], (err, response, body) => {
        twitterClient.post('media/upload', { media: new Buffer(body)})
        .then((res) => {
          twitterClient.post('statuses/update', { status: formattedTweet, media_ids: res.media_id_string })
          .then((result) => {
            sendMessage(`${dict.tweet.success}<https://twitter.com/${result.user.screen_name}/status/${result.id_str}>`);
            shareAnnouncement(result);
          })
          .catch((errors2) => handleTweetErrors(errors2));
        })
        .catch((errors) => handleTweetErrors(errors));
      });

     
    } else {
      twitterClient.post('statuses/update', { status: formattedTweet })
      .then((result) => {
        sendMessage(`${dict.tweet.success}<https://twitter.com/${result.user.screen_name}/status/${result.id_str}>`);
        shareAnnouncement(result);
      })
      .catch((errors) => handleTweetErrors(errors));
    }
  }

  /**
   * Handles the Twitter API errors
   * @param {*} errors 
   */
  function handleTweetErrors(errors) {
    errors.forEach((error) => {
      logActivity(new Date(), message.channel.name, message.guild.name, "ERROR", error.code + " — " + error.message);
    });
    sendMessage({
      content: dict.tweet.error,
      embed: {
        title: "Code(s) " + errors.map(error => error.code).toString(),
        footer: {
          text: footer()
        },
        description: errors.map(error => error.message).toString()
      }
    }, true);
  }

  /**
   * Posts a tweet to all announcement channels
   * @param {*} response 
   */
  function shareAnnouncement(response) {
    config.twitter.channels.forEach((id) => {
      var channel = bot.channels.get(id);
      var imageUrl = "";

      if(response.entities.media != null) {
        response.extended_entities.media.forEach((media) => {
          if (imageUrl.isEmpty()) {
            imageUrl = media.media_url_https;
          }
        });
      }

      channel.send(command.startsWith("q") ? "" : dict.tweet.tag, { 
        embed: { 
          title : dateFormat(response.created_at, "mmmm dS, yyyy — HH:MM"), 
          url : `https://twitter.com/${response.user.screen_name}/status/${response.id_str}`, 
          footer: {
            text: footer()
          },
          author : { 
            name : `${response.user.name} (@${response.user.screen_name})`, 
            url : `https://twitter.com/${response.user.screen_name}` 
          },
          image : {
            url : imageUrl
          },
          description : response.text 
        } 
      })
      .then((msg) => {
        msg.react("👍").then(() => {
          msg.react("👎");
        });
      })
      .catch((err) => {
        logActivity(new Date(), channel.name, channel.guild.name, "ERROR", err.message);
      });
    });
  }

  /**
   * Parrot sends a message
   * @param {*} text 
   * @param {boolean} hasExtraContent 
   */
  function sendMessage(text, hasExtraContent = false) {    
    message.channel.startTyping();
    if(hasExtraContent) {
      message.channel.send(text.content, text).catch((err) => catchError(err));
    } else {
      message.channel.send(text).catch((err) => catchError(err));
    }
    message.channel.stopTyping();

    function catchError(err) {
      logActivity(new Date(), message.channel.name, message.channel.guild.name, "ERROR", err.message);
    }
  }
});

/**
 * Helper function: Checks if the string is empty!
 */
String.prototype.isEmpty = function() {
  return (this.length === 0 || !this.trim());
};

/**
 * Gets the footer text for an embed.
 * @param {*} text
 */
function footer(text = config.organisation) {
  return text + " | " + dict.copyright;
}

/**
 * Logs activity
 * @param {Date} date 
 * @param {String} channel 
 * @param {String} server 
 * @param {String} displayName 
 * @param {String} text 
 */
function logActivity(date, channel, server, displayName, text) {
  var channelServer = channel.isEmpty() && server.isEmpty() ? "[BOT]" : `[#${channel} / ${server}]`;
  var logResult = `[${date.toLocaleString()}] ${channelServer} ${displayName} : ${text}`;

  console.log(logResult);
  
  if (config.log.fileLog) {
    fs.appendFile("log.txt", logResult + "\r\n", "utf8", function(err) {
      if(err) {
          return console.log(err);
      }
    });
  }

  config.log.channels.forEach(function(id) {
    bot.channels.get(id).send({ 
      embed: { 
        title : channel.isEmpty() && server.isEmpty() ? "BOT" : `#${channel} / ${server}`, 
        timestamp : date.toLocaleString(),
        footer: {
          text: footer()
        },
        author : { 
          name : displayName
        },
        description : text 
      } 
    });
  }, this);
}

bot.login(config.token);
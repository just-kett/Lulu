import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import asyncio
import os

from llm import ask_llm
from data import ban, roles

import server

load_dotenv()
bot_token = os.getenv("TOKEN")
handler = logging.FileHandler(filename="bot.log", encoding="utf-8", mode="w")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print("Kết nối tới bot thành công!")

@bot.event
async def on_member_join(member):
    await member.send(f"Welcome to our server, {member.mention}!<:smiling:1529907973088874506>")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    for i in ban:
        if i in message.content.lower():
            await message.delete()
            await message.channel.send(f"Hey {message.author.mention}, don't use that word<:smiling:1529907973088874506>")

    if "hey remi" in message.content.lower() or "remi ơi" in message.content.lower(): 
        prompt = message.content
        answer = await asyncio.to_thread(
            ask_llm,
            prompt                            
        )
        await message.reply(answer)

    await bot.process_commands(message)

@bot.command()
async def Assign(ctx, *, input):
    for i in roles:
        if i == input:
            role = discord.utils.get(ctx.guild.roles, name=i)
    if role:
        await ctx.channel.send(f"{ctx.author.mention} is now assigned to {role}.")
    else:
        await ctx.channel.send(f"Role doens't exist!")

@bot.command()
async def ask(ctx, *, prompt):
    answer = await asyncio.to_thread(
        ask_llm,
        prompt
    )
    await ctx.reply(answer)

server.keep_alive()
bot.run(token=bot_token, log_handler=handler, log_level=logging.DEBUG)
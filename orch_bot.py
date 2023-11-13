# This example requires the 'message_content' intent.

import discord
from discord.ext import commands
from sheet import Sheet
from draw import PlayerBoxDrawer
from PIL import Image, ImageDraw, ImageFont

your_bot_token = "your bot token here"

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='$', intents=intents)

program_list = Sheet.PROGRAM_LIST
part_list = Sheet.PART_LIST
sheet_list = {program: Sheet(program) for program in program_list}

@bot.event
async def on_ready():
    print("Ready!")
    for program in sheet_list:
        sheet_list[program].load_csv()

@bot.event
async def on_message(message):
    channel = message.channel
    guild = message.guild

    # カレンダーチャネルへの対応
    calender_channel = discord.utils.get(guild.channels, name="カレンダー")
    if calender_channel == channel:
        shusseki_emoji = discord.utils.get(bot.emojis, name="shusseki")
        kesseki_emoji = discord.utils.get(bot.emojis, name="kesseki")
        chikoku_emoji = discord.utils.get(bot.emojis, name="chikoku")
        soutai_emoji = discord.utils.get(bot.emojis, name="soutai")
        await message.add_reaction(shusseki_emoji)
        await message.add_reaction(kesseki_emoji)
        await message.add_reaction(chikoku_emoji)
        await message.add_reaction(soutai_emoji)
        
    await bot.process_commands(message) # 重要! これが無いと @bot.command()が動作しなくなる
    
@bot.event
async def on_raw_reaction_add(payLoad):
    guild = discord.utils.get(bot.guilds, id=payLoad.guild_id)
    channel = discord.utils.get(guild.channels, id=payLoad.channel_id)
    push_member = payLoad.member
    pushed_emoji = payLoad.emoji

    # 出力絵文字対応
    calender_channel = discord.utils.get(guild.channels, name="カレンダー")
    output_emoji = discord.utils.get(bot.emojis, name="shutsuryoku")
    output_channel = discord.utils.get(guild.channels, name="出欠表出力")
    
    output_emoji_flag = (output_emoji == pushed_emoji) # 出力の絵文字が押された
    calender_channel_flag = (calender_channel == channel) # カレンダーチャンネルで押された

    if output_emoji_flag and calender_channel_flag:
        unnei_role = discord.utils.get(guild.roles, name="運営")
        message = await channel.fetch_message(payLoad.message_id)
        reactions = message.reactions
        
        if unnei_role in push_member.roles: # 押した人が運営ロール
            shusseki_emoji = discord.utils.get(bot.emojis, name="shusseki")
            shusseki_reaction = discord.utils.get(reactions, emoji=shusseki_emoji)
            if shusseki_reaction == None:
                raise ValueError(f"絵文字{shusseki_emoji} が一つも押されていません")
            shusseki_members = [user async for user in shusseki_reaction.users()]

            kesseki_emoji = discord.utils.get(bot.emojis, name="kesseki")
            kesseki_reaction = discord.utils.get(reactions, emoji=kesseki_emoji)
            if kesseki_reaction == None:
                raise ValueError(f"絵文字{kesseki_emoji} が一つも押されていません")
            kesseki_members = [user async for user in kesseki_reaction.users()]

            soutai_emoji = discord.utils.get(bot.emojis, name="soutai")
            soutai_reaction = discord.utils.get(reactions, emoji=soutai_emoji)
            if soutai_reaction == None:
                raise ValueError(f"絵文字{soutai_emoji} が一つも押されていません")
            soutai_members = [user async for user in soutai_reaction.users()]

            chikoku_emoji = discord.utils.get(bot.emojis, name="chikoku")
            chikoku_reaction = discord.utils.get(reactions, emoji=chikoku_emoji)
            if chikoku_reaction == None:
                raise ValueError(f"絵文字{chikoku_emoji} が一つも押されていません")
            chikoku_members = [user async for user in chikoku_reaction.users()]

            for program in program_list:
                boxdrawer = PlayerBoxDrawer("対向配置")
                sheet = sheet_list[program]
                for member_id in sheet.sheet_dict:
                    member = discord.utils.get(guild.members, id=member_id)
                    if member in shusseki_members:
                        boxdrawer.draw_playerbox(sheet.sheet_dict[member_id][0], sheet.sheet_dict[member_id][1], sheet.sheet_dict[member_id][2], PlayerBoxDrawer.GREEN, PlayerBoxDrawer.BLACK)
                    elif member in kesseki_members:
                        boxdrawer.draw_playerbox(sheet.sheet_dict[member_id][0], sheet.sheet_dict[member_id][1], sheet.sheet_dict[member_id][2], PlayerBoxDrawer.GRAY, PlayerBoxDrawer.BLACK)
                    elif member in chikoku_members:
                        boxdrawer.draw_playerbox(sheet.sheet_dict[member_id][0], sheet.sheet_dict[member_id][1], sheet.sheet_dict[member_id][2], PlayerBoxDrawer.YELLOW, PlayerBoxDrawer.BLACK)
                    elif member in soutai_members:
                        boxdrawer.draw_playerbox(sheet.sheet_dict[member_id][0], sheet.sheet_dict[member_id][1], sheet.sheet_dict[member_id][2], PlayerBoxDrawer.BLUE, PlayerBoxDrawer.BLACK)
                    else:
                        boxdrawer.draw_playerbox(sheet.sheet_dict[member_id][0], sheet.sheet_dict[member_id][1], sheet.sheet_dict[member_id][2], PlayerBoxDrawer.WHITE, PlayerBoxDrawer.RED)
                
                boxdrawer.draw_program(program)
                boxdrawer.save(program + ".png")
                with open(program + ".png", 'rb') as f:
                    d_file = discord.File(f, description=program)
                    await output_channel.send(file=d_file)
                    
        else: # 押した人が運営ロールでない場合
            shutsuryoku_reaction = discord.utils.get(message.reactions, emoji=output_emoji)
            await shutsuryoku_reaction.remove(member) # リアクションを削除
            await channel.send("出力は運営専用です", ephemeral=True) # ユーザーのみに見えるメッセージ

@bot.command()
async def show_pultlist(ctx):
    if ctx.channel.name == "出欠管理システム": # 特定channelのみで動作
        for program in sheet_list:
            boxdrawer = PlayerBoxDrawer("対向配置")
            sheet = sheet_list[program]
            for member_id in sheet.sheet_dict:
                pult_info = sheet.sheet_dict[member_id]
                boxdrawer.draw_playerbox(pult_info[0], pult_info[1], pult_info[2], PlayerBoxDrawer.WHITE, PlayerBoxDrawer.BLACK)
            
            
            boxdrawer.draw_program(program)
            boxdrawer.save(program + ".png")
            with open(program + ".png", 'rb') as f:
                d_file = discord.File(f, description=program)
                await ctx.send(file=d_file)
            
@bot.command()
async def append(ctx, member: discord.Member, program, part, num: int):
    if ctx.channel.name == "出欠管理システム": # 特定channelのみで動作
        if(program not in program_list):
            raise ValueError(f"program: {program} が見つかりません")
        if(part not in part_list):
            raise ValueError(f"part: {part} が見つかりません")
        if discord.utils.get(ctx.guild.roles, name="第９回演奏会 参加者") in member.roles: # 演奏会参加者のロールに限定
            sheet_list[program].append(part, num, member)
        else:
            role = discord.utils.get(ctx.guild.roles, name="第９回演奏会 参加者")
            raise ValueError(f"member: {member} はロール{role} がありません")
        await save(ctx)
    
@bot.command()
async def save(ctx):
    if ctx.channel.name == "出欠管理システム": # 特定channelのみで動作
        for program in sheet_list:
            sheet_list[program].save_csv()

@bot.command()
async def clear(ctx):
    if ctx.channel.name == "出欠管理システム": # 特定channelのみで動作
        for program in sheet_list:
            sheet_list[program].clear()
            
        await save(ctx)

@bot.command()
async def delete(ctx, program, member: discord.Member):
    if ctx.channel.name == "出欠管理システム": # 特定channelのみで動作
        if program not in program_list:
            raise ValueError(f"program: {program} が見つかりません")
        sheet_list[program].delete(member)
        await save(ctx)

    
@bot.event
async def on_command_error(ctx, error):
    if ctx.channel.name == "出欠管理システム": # 特定channelのみで動作
        ch = ctx.channel
        command = ctx.command
        await ch.send(f'{command}は失敗しました。エラー: {error}')

@bot.event
async def on_command_completion(ctx):
    if ctx.channel.name == "出欠管理システム": # 特定channelのみで動作
        await ctx.message.add_reaction('✅')
    
bot.run(your_bot_token)

import discord
from discord.ext import commands
from sheet import Sheet
from draw import PlayerBoxDrawer
from PIL import Image, ImageDraw, ImageFont
import configparser

config = configparser.ConfigParser()
config.read('settings.ini')

your_bot_token = config['TOKEN']['bot_token']
print(your_bot_token)
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='$', intents=intents)

program_list = [program for program in config["PROGRAM"].values()]
part_list = Sheet.PART_LIST
sheet_list = {program: Sheet(program) for program in program_list}

command_channel_name_list = [channel for channel in config["COMMAND_CHANNEL"].values()]
output_channel_name  = config["OUTPUT_CHANNEL"]["output_channel"]
rsvp_channel_name_list    = [channel for channel in config["RSVP_CHANNEL"].values()]

output_role_name_list = [role for role in config["ROLE"].values()]

shusseki_name = config["EMOJI"]["shusseki"]
kesseki_name  = config["EMOJI"]["kesseki"]
chikoku_name  = config["EMOJI"]["chikoku"]
soutai_name   = config["EMOJI"]["soutai"]
output_name   = config["EMOJI"]["output"]

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
    rsvp_channels = [discord.utils.get(guild.channels, name=rsvp_channel_name)
                     for rsvp_channel_name in rsvp_channel_name_list]
    
    if channel in rsvp_channels:
        shusseki_emoji = discord.utils.get(bot.emojis, name=shusseki_name)
        kesseki_emoji = discord.utils.get(bot.emojis, name=kesseki_name)
        chikoku_emoji = discord.utils.get(bot.emojis, name=chikoku_name)
        soutai_emoji = discord.utils.get(bot.emojis, name=soutai_name)
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
    # calender_channel = discord.utils.get(guild.channels, name="カレンダー")
    rsvp_channel_list = [discord.utils.get(guild.channels, name=rsvp_channel_name) for rsvp_channel_name in rsvp_channel_name_list]
    
    output_emoji = discord.utils.get(bot.emojis, name=output_name)
    output_channel = discord.utils.get(guild.channels, name=output_channel_name)
    
    output_emoji_flag = (output_emoji == pushed_emoji) # 出力の絵文字が押された
    rsvp_channel_flag = (channel in rsvp_channel_list) # RSVPチャンネルで押された

    if output_emoji_flag and rsvp_channel_flag:
        output_role_list = [discord.utils.get(guild.roles, name=output_role_name)
                            for output_role_name in output_role_name_list]
        message = await channel.fetch_message(payLoad.message_id)
        reactions = message.reactions

        has_output_authority = (len(output_role_list + push_member.roles)
                                != len(set(output_role_list + push_member.roles)))
        
        if has_output_authority: # 押した人が運営ロール
            shusseki_emoji = discord.utils.get(bot.emojis, name=shusseki_name)
            shusseki_reaction = discord.utils.get(reactions, emoji=shusseki_emoji)
            if shusseki_reaction == None:
                raise ValueError(f"絵文字{shusseki_emoji} が一つも押されていません")
            shusseki_members = [user async for user in shusseki_reaction.users()]

            kesseki_emoji = discord.utils.get(bot.emojis, name=kesseki_name)
            kesseki_reaction = discord.utils.get(reactions, emoji=kesseki_emoji)
            if kesseki_reaction == None:
                raise ValueError(f"絵文字{kesseki_emoji} が一つも押されていません")
            kesseki_members = [user async for user in kesseki_reaction.users()]

            soutai_emoji = discord.utils.get(bot.emojis, name=soutai_name)
            soutai_reaction = discord.utils.get(reactions, emoji=soutai_emoji)
            if soutai_reaction == None:
                raise ValueError(f"絵文字{soutai_emoji} が一つも押されていません")
            soutai_members = [user async for user in soutai_reaction.users()]

            chikoku_emoji = discord.utils.get(bot.emojis, name=chikoku_name)
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
            await shutsuryoku_reaction.remove(push_member) # リアクションを削除

@bot.command()
async def show_pultlist(ctx):
    if ctx.channel.name in command_channel_name_list: # 特定channelのみで動作
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
    if ctx.channel.name in command_channel_name_list: # 特定channelのみで動作
        if(program not in program_list):
            raise ValueError(f"program: {program} が見つかりません")
        if(part not in part_list):
            raise ValueError(f"part: {part} が見つかりません")
        sheet_list[program].append(part, num, member)
        await save(ctx)
    
@bot.command()
async def save(ctx):
    if ctx.channel.name in command_channel_name_list: # 特定channelのみで動作
        for program in sheet_list:
            sheet_list[program].save_csv()

@bot.command()
async def clear(ctx):
    if ctx.channel.name in command_channel_name_list: # 特定channelのみで動作
        for program in sheet_list:
            sheet_list[program].clear()
            
        await save(ctx)

@bot.command()
async def delete(ctx, program, member: discord.Member):
    if ctx.channel.name in command_channel_name_list: # 特定channelのみで動作
        if program not in program_list:
            raise ValueError(f"program: {program} が見つかりません")
        if member.id not in sheet_list[program].sheet_dict:
            raise ValueError(f"member: {member} が見つかりません")
        sheet_list[program].delete(member)
        await save(ctx)

    
@bot.event
async def on_command_error(ctx, error):
    if ctx.channel.name in command_channel_name_list: # 特定channelのみで動作
        ch = ctx.channel
        command = ctx.command
        await ch.send(f'{command}は失敗しました。エラー: {error}')

@bot.event
async def on_command_completion(ctx):
    if ctx.channel.name in command_channel_name_list: # 特定channelのみで動作
        await ctx.message.add_reaction('✅')
    
bot.run(your_bot_token)

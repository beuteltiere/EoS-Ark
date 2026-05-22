import discord
from discord.ext import tasks, commands
import aiohttp
from datetime import datetime
import os
import json
from dotenv import load_dotenv, set_key

load_dotenv()

ENV_FILE     = '.env'
SERVERS_FILE = 'servers.json'
COLORS_FILE  = 'colors.json'   # { "PlayerName": {"color": "red", "note": "..."} }

DISCORD_TOKEN  = os.getenv('DISCORD_TOKEN')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '5'))

ADMIN_IDS_RAW = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(',') if x.strip().isdigit()}

# ─── ANSI colour map ─────────────────────────────────────────────────────────
# Discord renders these inside ```ansi code blocks.
ANSI_COLORS = {
    'red':     '\u001b[31m',
    'green':   '\u001b[32m',
    'yellow':  '\u001b[33m',
    'blue':    '\u001b[34m',
    'pink':    '\u001b[35m',
    'cyan':    '\u001b[36m',
    'white':   '\u001b[37m',
    'orange':  '\u001b[38;5;214m',
    'gray':    '\u001b[30m',
}
ANSI_RESET = '\u001b[0m'
COLOR_NAMES = list(ANSI_COLORS.keys())   # for help text


# ─── Persistence helpers ─────────────────────────────────────────────────────

def load_servers() -> dict:
    if os.path.exists(SERVERS_FILE):
        with open(SERVERS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_servers(servers: dict):
    with open(SERVERS_FILE, 'w') as f:
        json.dump(servers, f, indent=2)

def load_colors() -> dict:
    """Load colors.json, migrating old {"name": "color"} entries to {"name": {"color":..., "note":""}}."""
    if not os.path.exists(COLORS_FILE):
        return {}
    with open(COLORS_FILE, 'r') as f:
        raw = json.load(f)
    # Migrate any legacy string values
    migrated = False
    for k, v in raw.items():
        if isinstance(v, str):
            raw[k] = {'color': v, 'note': ''}
            migrated = True
    if migrated:
        save_colors(raw)
    return raw

def save_colors(colors: dict):
    with open(COLORS_FILE, 'w') as f:
        json.dump(colors, f, indent=2)


# ─── Config Cog ──────────────────────────────────────────────────────────────

class ConfigCog(commands.Cog):
    def __init__(self, bot: 'BattleMetricsBot'):
        self.bot = bot

    def _is_admin(self, user_id: int) -> bool:
        return not ADMIN_IDS or user_id in ADMIN_IDS

    # ── Server management ────────────────────────────────────────────────────

    @commands.command(name='addserver')
    async def add_server(self, ctx, server_id: str, channel: discord.TextChannel = None):
        """Start monitoring a BattleMetrics server.
        Usage: !addserver 37822103
               !addserver 37822103 #my-channel"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return

        target = channel or ctx.channel

        if server_id in self.bot.servers:
            await ctx.send(f'```⚠️ Server {server_id} is already monitored.```')
            return

        self.bot.servers[server_id] = {'channel_id': target.id, 'message_id': None}
        save_servers(self.bot.servers)

        await ctx.send(f'```✅ Added server {server_id} → #{target.name}. Fetching status...```')
        data = await self.bot.fetch_server_data(server_id)
        if data:
            msg = await target.send(self.bot.format_server_message(data))
            self.bot.servers[server_id]['message_id'] = msg.id
            save_servers(self.bot.servers)
            self.bot.player_state[server_id] = {
                p.get('id') for p in data.get('included', [])
                if p.get('type') == 'player' and p.get('id')
            }
        else:
            await ctx.send(f'```❌ Could not fetch data for server {server_id}. Check the ID.```')

    @commands.command(name='removeserver')
    async def remove_server(self, ctx, server_id: str):
        """Stop monitoring a server.  Usage: !removeserver 37822103"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        if server_id not in self.bot.servers:
            await ctx.send(f'```⚠️ Server {server_id} not found.```')
            return
        del self.bot.servers[server_id]
        self.bot.player_state.pop(server_id, None)
        save_servers(self.bot.servers)
        await ctx.send(f'```✅ Removed server {server_id}.```')

    @commands.command(name='listservers')
    async def list_servers(self, ctx):
        """List all monitored servers.  Usage: !listservers"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        if not self.bot.servers:
            await ctx.send('```ℹ️ No servers configured. Use !addserver <id>.```')
            return
        lines = [f"{'─'*44}", ' Monitored servers', f"{'─'*44}"]
        for sid, cfg in self.bot.servers.items():
            ch = self.bot.get_channel(cfg['channel_id'])
            ch_name = f'#{ch.name}' if ch else f"ID {cfg['channel_id']}"
            has_msg = '✓ message exists' if cfg.get('message_id') else '✗ no message yet'
            lines.append(f' {sid}  →  {ch_name}  [{has_msg}]')
        lines.append(f"{'─'*44}")
        await ctx.send('```' + '\n'.join(lines) + '```')

    # ── Colour management ────────────────────────────────────────────────────

    @commands.command(name='colorname')
    async def color_name(self, ctx, color: str, *, player_name: str):
        """Assign a colour to a player name.
        Usage: !colorname red SomePlayer
        Colors: red, green, yellow, blue, pink, cyan, white, orange, gray"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        color = color.lower()
        if color not in ANSI_COLORS:
            await ctx.send(
                f'```⚠️ Unknown color "{color}".\n'
                f'Available: {", ".join(COLOR_NAMES)}```'
            )
            return
        entry = self.bot.colors.get(player_name, {'color': color, 'note': ''})
        entry['color'] = color
        self.bot.colors[player_name] = entry
        save_colors(self.bot.colors)
        note_hint = f'  (note: {entry["note"]})' if entry.get('note') else ''
        await ctx.send(f'```✅ "{player_name}" will now appear in {color}.{note_hint}```')

    @commands.command(name='uncolorname')
    async def uncolor_name(self, ctx, *, player_name: str):
        """Remove a colour tag from a player name.
        Usage: !uncolorname SomePlayer"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        if player_name not in self.bot.colors:
            await ctx.send(f'```⚠️ No color set for "{player_name}".```')
            return
        del self.bot.colors[player_name]
        save_colors(self.bot.colors)
        await ctx.send(f'```✅ Color and note removed from "{player_name}".```')

    @commands.command(name='listcolors')
    async def list_colors(self, ctx):
        """List all colored player names.  Usage: !listcolors"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        if not self.bot.colors:
            await ctx.send('```ℹ️ No colored names set. Use !colorname <color> <name>.```')
            return
        lines = [f"{'─'*48}", ' Colored names', f"{'─'*48}"]
        for name, entry in sorted(self.bot.colors.items()):
            if isinstance(entry, str):
                entry = {'color': entry, 'note': ''}
            color    = entry.get('color', '?')
            note     = entry.get('note', '')
            note_str = f'  ← {note}' if note else ''
            lines.append(f' {color:<8}  {name}{note_str}')
        lines.append(f"{'─'*48}")
        await ctx.send('```' + '\n'.join(lines) + '```')


    @commands.command(name='setnote')
    async def set_note(self, ctx, player_name: str, *, note: str):
        """Add or update a note for a colored player name.
        Usage: !setnote SomePlayer known griefer, watch out"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        if player_name not in self.bot.colors:
            await ctx.send(
                f'```⚠️ "{player_name}" has no color entry.\n'
                f'Use !colorname <color> {player_name} first.```'
            )
            return
        entry = self.bot.colors[player_name]
        if isinstance(entry, str):
            entry = {'color': entry, 'note': ''}
        entry['note'] = note
        self.bot.colors[player_name] = entry
        save_colors(self.bot.colors)
        await ctx.send(f'```✅ Note set for "{player_name}": {note}```')

    @commands.command(name='removenote')
    async def remove_note(self, ctx, *, player_name: str):
        """Remove the note from a colored player name (keeps the color).
        Usage: !removenote SomePlayer"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        if player_name not in self.bot.colors:
            await ctx.send(f'```⚠️ "{player_name}" not found.```')
            return
        entry = self.bot.colors[player_name]
        if isinstance(entry, dict):
            entry['note'] = ''
            self.bot.colors[player_name] = entry
            save_colors(self.bot.colors)
        await ctx.send(f'```✅ Note removed from "{player_name}".```')

    # ── Other ────────────────────────────────────────────────────────────────

    @commands.command(name='status')
    async def force_status(self, ctx, server_id: str = None):
        """Force an immediate update.
        Usage: !status            (all servers)
               !status 37822103  (one server)"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        targets = [server_id] if server_id else list(self.bot.servers.keys())
        if not targets:
            await ctx.send('```⚠️ No servers configured.```')
            return
        await ctx.send(f'```🔄 Updating {len(targets)} server(s)...```')
        for sid in targets:
            if sid not in self.bot.servers:
                await ctx.send(f'```⚠️ Server {sid} not found.```')
            else:
                await self.bot.update_server(sid)

    @commands.command(name='setinterval')
    async def set_interval(self, ctx, minutes: int):
        """Set poll interval in minutes (1-60).  Usage: !setinterval 5"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        if not 1 <= minutes <= 60:
            await ctx.send('```⚠️ Must be 1–60.```')
            return
        self.bot.check_interval = minutes
        set_key(ENV_FILE, 'CHECK_INTERVAL', str(minutes))
        self.bot.monitor_loop.cancel()
        self.bot.monitor_loop.change_interval(minutes=minutes)
        self.bot.monitor_loop.start()
        await ctx.send(f'```✅ Interval set to {minutes} minute(s). Loop restarted.```')

    @commands.command(name='addadmin')
    async def add_admin(self, ctx, user: discord.User):
        """Grant config permissions.  Usage: !addadmin @User"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        ADMIN_IDS.add(user.id)
        set_key(ENV_FILE, 'ADMIN_IDS', ','.join(str(i) for i in ADMIN_IDS))
        await ctx.send(f'```✅ {user.name} ({user.id}) added as admin.```')

    @commands.command(name='removeadmin')
    async def remove_admin(self, ctx, user: discord.User):
        """Revoke config permissions.  Usage: !removeadmin @User"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        ADMIN_IDS.discard(user.id)
        set_key(ENV_FILE, 'ADMIN_IDS', ','.join(str(i) for i in ADMIN_IDS))
        await ctx.send(f'```✅ {user.name} removed from admins.```')

    @commands.command(name='settings')
    async def show_settings(self, ctx):
        """Show all current settings.  Usage: !settings"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        admins = ', '.join(str(i) for i in ADMIN_IDS) if ADMIN_IDS else 'None (all users)'
        msg = (
            f"```\n{'─'*42}\n Bot Settings\n{'─'*42}\n"
            f" CHECK_INTERVAL : {self.bot.check_interval} minute(s)\n"
            f" ADMIN_IDS      : {admins}\n"
            f" Servers        : {len(self.bot.servers)} configured\n"
            f" Colored names  : {len(self.bot.colors)} configured\n"
            f"{'─'*42}\n Commands\n{'─'*42}\n"
            f" !addserver <id> [#ch]        Add a server\n"
            f" !removeserver <id>           Remove a server\n"
            f" !listservers                 List all servers\n"
            f" !colorname <color> <name>    Color a player name\n"
            f" !uncolorname <name>          Remove color + note\n"
            f" !setnote <name> <note>       Add/update a note\n"
            f" !removenote <name>           Remove note (keep color)\n"
            f" !listcolors                  List names, colors & notes\n"
            f" !status [id]                 Force update (all/one)\n"
            f" !setinterval <mins>          Poll interval (1-60)\n"
            f" !addadmin / !removeadmin     Admin management\n"
            f" !settings                    This panel\n"
            f"{'─'*42}\n Colors: {', '.join(COLOR_NAMES)}\n{'─'*42}```"
        )
        await ctx.send(msg)


# ─── Bot ─────────────────────────────────────────────────────────────────────

class BattleMetricsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.check_interval  = CHECK_INTERVAL
        self.servers: dict   = load_servers()
        self.colors: dict    = load_colors()    # { "PlayerName": "red" }
        self.player_state: dict = {}
        self.last_content: dict  = {}   # skip edits when content unchanged

    async def setup_hook(self):
        await self.add_cog(ConfigCog(self))
        self.monitor_loop.change_interval(minutes=self.check_interval)
        self.monitor_loop.start()

    async def on_ready(self):
        print(f'Logged in as {self.user}')
        print(f'Monitoring {len(self.servers)} server(s) every {self.check_interval} min')
        print(f'Colored names: {len(self.colors)}')

    # ── API ──────────────────────────────────────────────────────────────────

    async def fetch_server_data(self, server_id: str):
        url = (
            f'https://api.battlemetrics.com/servers/{server_id}'
            '?include=player,identifier'
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        print(f'[{server_id}] API error {resp.status}')
                        return None
                    return await resp.json()
        except Exception as e:
            print(f'[{server_id}] Fetch error: {e}')
            return None

    # ── Formatting ───────────────────────────────────────────────────────────

    def _apply_color(self, name: str) -> tuple[str, str]:
        """Return (display_name_with_ansi, note) for a player name."""
        entry = self.colors.get(name)
        if not entry:
            return name, ''
        if isinstance(entry, str):          # legacy fallback
            entry = {'color': entry, 'note': ''}
        color_key = entry.get('color', '')
        note      = entry.get('note', '')
        if color_key in ANSI_COLORS:
            display = f'{ANSI_COLORS[color_key]}{name}{ANSI_RESET}'
        else:
            display = name
        return display, note

    def format_server_message(self, data) -> tuple[str, str]:
        """Return (full_discord_content, body_without_timestamp).
        The body key is used to detect real changes so we skip edits when
        only the clock ticked but nothing else changed."""
        try:
            attrs        = data['data']['attributes']
            name         = attrs.get('name', 'Unknown')
            player_count = attrs.get('players', 0)
            max_players  = attrs.get('maxPlayers', 0)
            status       = attrs.get('status', 'offline')
            updated      = datetime.now().strftime('%H:%M:%S')

            players = [
                item for item in data.get('included', [])
                if item.get('type') == 'player'
            ]

            lines = []
            for idx, player in enumerate(players, 1):
                try:
                    pname = player.get('attributes', {}).get('name', 'Unknown')
                    secs  = 0
                    for item in player.get('meta', {}).get('metadata', []):
                        if item.get('key') == 'time':
                            secs = int(item.get('value', 0) or 0)
                            break
                    h, m = secs // 3600, (secs % 3600) // 60
                    t = f'{h}h {m}m' if h else f'{m}m'
                    colored_name, note = self._apply_color(pname)
                    note_str = f'  ← {note}' if note else ''
                    lines.append(f'[ {idx} | {colored_name} | {t} ]{note_str}')
                except Exception as e:
                    print(f'Player parse error: {e}')

            body = '\n'.join(lines) if lines else 'No players online'
            # change_key excludes the timestamp so we can diff without false positives
            change_key = f'{name}|{status}|{player_count}/{max_players}|{body}'

            content = (
                f'```ansi\n'
                f'Server:  {name}\n'
                f'Status:  {status}\n'
                f'Players: {player_count}/{max_players}\n'
                f'Updated: {updated}\n\n'
                f'{body}\n```'
            )
            return content, change_key
        except Exception as e:
            print(f'Format error: {e}')
            err = f'```Error formatting data: {e}```'
            return err, err

    # ── Update one server ─────────────────────────────────────────────────────

    async def update_server(self, server_id: str):
        cfg = self.servers.get(server_id)
        if not cfg:
            return

        data = await self.fetch_server_data(server_id)
        if not data:
            return

        # Track player changes
        players = [
            item for item in data.get('included', [])
            if item.get('type') == 'player'
        ]
        current  = {p.get('id') for p in players if p.get('id')}
        previous = self.player_state.get(server_id, current)
        joined   = current - previous
        left     = previous - current
        if joined or left:
            parts = []
            if joined: parts.append(f'{len(joined)} joined')
            if left:   parts.append(f'{len(left)} left')
            print(f'[{server_id}] {", ".join(parts)}')
        self.player_state[server_id] = current

        channel = self.get_channel(cfg['channel_id'])
        if not channel:
            print(f'[{server_id}] Channel {cfg["channel_id"]} not found')
            return

        content, change_key = self.format_server_message(data)

        # ── Skip the Discord API call when nothing real changed ───────────────
        if self.last_content.get(server_id) == change_key:
            print(f'[{server_id}] No changes — skipping edit')
            return
        self.last_content[server_id] = change_key

        # Edit existing message using a lightweight partial (no extra GET).
        # Falls through to send() only if the message no longer exists.
        if cfg.get('message_id'):
            try:
                await channel.get_partial_message(cfg['message_id']).edit(content=content)
                return
            except discord.NotFound:
                print(f'[{server_id}] Message gone — sending a new one')
            except discord.Forbidden:
                print(f'[{server_id}] No edit permission')
                return

        msg = await channel.send(content)
        self.servers[server_id]['message_id'] = msg.id
        save_servers(self.servers)

    # ── Monitor loop ─────────────────────────────────────────────────────────

    @tasks.loop(minutes=5)
    async def monitor_loop(self):
        import asyncio
        servers = list(self.servers.keys())
        print(f'Poll cycle: {len(servers)} server(s)')
        for server_id in servers:
            await self.update_server(server_id)
            await asyncio.sleep(3)   # 3 s stagger: 13 servers = ~39 s total, well within 5 min

    @monitor_loop.before_loop
    async def before_monitor(self):
        import asyncio
        await self.wait_until_ready()
        servers = list(self.servers.keys())
        print(f'Starting monitor for {len(servers)} server(s)...')
        for server_id in servers:
            data = await self.fetch_server_data(server_id)
            if data:
                players = [
                    p for p in data.get('included', [])
                    if p.get('type') == 'player'
                ]
                self.player_state[server_id] = {
                    p.get('id') for p in players if p.get('id')
                }
                await self.update_server(server_id)
            await asyncio.sleep(3)   # stagger startup edits same as the main loop


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    bot = BattleMetricsBot()
    bot.run(DISCORD_TOKEN)

if __name__ == '__main__':
    main()
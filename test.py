import discord
from discord.ext import tasks, commands
import aiohttp
from datetime import datetime
import os
import json
from dotenv import load_dotenv, set_key

load_dotenv()

ENV_FILE     = '.env'
SERVERS_FILE = 'servers.json'   # stores all monitored servers + their message IDs

DISCORD_TOKEN  = os.getenv('DISCORD_TOKEN')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '5'))

ADMIN_IDS_RAW = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(',') if x.strip().isdigit()}


# ─── Server store ────────────────────────────────────────────────────────────
# servers.json structure:
# {
#   "<battlemetrics_server_id>": {
#     "channel_id": 123456789,
#     "message_id": null        ← filled in after first post
#   },
#   ...
# }

def load_servers() -> dict:
    if os.path.exists(SERVERS_FILE):
        with open(SERVERS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_servers(servers: dict):
    with open(SERVERS_FILE, 'w') as f:
        json.dump(servers, f, indent=2)


# ─── Config Cog ──────────────────────────────────────────────────────────────

class ConfigCog(commands.Cog):
    def __init__(self, bot: 'BattleMetricsBot'):
        self.bot = bot

    def _is_admin(self, user_id: int) -> bool:
        return not ADMIN_IDS or user_id in ADMIN_IDS

    # ── !addserver ───────────────────────────────────────────────────────────

    @commands.command(name='addserver')
    async def add_server(self, ctx, server_id: str, channel: discord.TextChannel = None):
        """Start monitoring a BattleMetrics server.
        Usage: !addserver 37822103            (posts in current channel)
               !addserver 37822103 #my-channel"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return

        target_channel = channel or ctx.channel

        if server_id in self.bot.servers:
            await ctx.send(f'```⚠️ Server {server_id} is already being monitored.```')
            return

        self.bot.servers[server_id] = {
            'channel_id': target_channel.id,
            'message_id': None,
        }
        save_servers(self.bot.servers)

        # Fetch and post the first status message immediately
        await ctx.send(f'```✅ Added server {server_id} → #{target_channel.name}. Fetching status...```')
        data = await self.bot.fetch_server_data(server_id)
        if data:
            msg = await target_channel.send(self.bot.format_server_message(data))
            self.bot.servers[server_id]['message_id'] = msg.id
            save_servers(self.bot.servers)
            # Seed player tracking
            self.bot.player_state[server_id] = {
                p.get('id') for p in data.get('included', [])
                if p.get('type') == 'player' and p.get('id')
            }
        else:
            await ctx.send(f'```❌ Could not fetch data for server {server_id}. Check the ID.```')

    # ── !removeserver ────────────────────────────────────────────────────────

    @commands.command(name='removeserver')
    async def remove_server(self, ctx, server_id: str):
        """Stop monitoring a server.
        Usage: !removeserver 37822103"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return

        if server_id not in self.bot.servers:
            await ctx.send(f'```⚠️ Server {server_id} is not in the list.```')
            return

        del self.bot.servers[server_id]
        self.bot.player_state.pop(server_id, None)
        save_servers(self.bot.servers)
        await ctx.send(f'```✅ Removed server {server_id}.```')

    # ── !listservers ─────────────────────────────────────────────────────────

    @commands.command(name='listservers')
    async def list_servers(self, ctx):
        """List all monitored servers.
        Usage: !listservers"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return

        if not self.bot.servers:
            await ctx.send('```ℹ️ No servers configured. Use !addserver <id> [#channel].```')
            return

        lines = [f"{'─'*44}", ' Monitored servers', f"{'─'*44}"]
        for sid, cfg in self.bot.servers.items():
            ch = self.bot.get_channel(cfg['channel_id'])
            ch_name  = f'#{ch.name}' if ch else f"ID {cfg['channel_id']}"
            has_msg  = '✓ message pinned' if cfg.get('message_id') else '✗ no message yet'
            lines.append(f' {sid}  →  {ch_name}  [{has_msg}]')
        lines.append(f"{'─'*44}")
        await ctx.send('```' + '\n'.join(lines) + '```')

    # ── !status ──────────────────────────────────────────────────────────────

    @commands.command(name='status')
    async def force_status(self, ctx, server_id: str = None):
        """Force an immediate update for one or all servers.
        Usage: !status             (updates all)
               !status 37822103   (updates one)"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return

        targets = ([server_id] if server_id else list(self.bot.servers.keys()))
        if not targets:
            await ctx.send('```⚠️ No servers configured. Use !addserver <id> first.```')
            return

        await ctx.send(f'```🔄 Updating {len(targets)} server(s)...```')
        for sid in targets:
            if sid not in self.bot.servers:
                await ctx.send(f'```⚠️ Server {sid} not found.```')
                continue
            await self.bot.update_server(sid)

    # ── !setinterval ─────────────────────────────────────────────────────────

    @commands.command(name='setinterval')
    async def set_interval(self, ctx, minutes: int):
        """Set poll interval in minutes (1-60).
        Usage: !setinterval 5"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        if not 1 <= minutes <= 60:
            await ctx.send('```⚠️ Must be 1–60 minutes.```')
            return
        self.bot.check_interval = minutes
        set_key(ENV_FILE, 'CHECK_INTERVAL', str(minutes))
        self.bot.monitor_loop.cancel()
        self.bot.monitor_loop.change_interval(minutes=minutes)
        self.bot.monitor_loop.start()
        await ctx.send(f'```✅ Interval set to {minutes} minute(s). Loop restarted.```')

    # ── !addadmin / !removeadmin ─────────────────────────────────────────────

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

    # ── !settings ────────────────────────────────────────────────────────────

    @commands.command(name='settings')
    async def show_settings(self, ctx):
        """Show all current settings.  Usage: !settings"""
        if not self._is_admin(ctx.author.id):
            await ctx.send('```⛔ No permission.```')
            return
        admins = ', '.join(str(i) for i in ADMIN_IDS) if ADMIN_IDS else 'None (all users)'
        msg = (
            f"```\n{'─'*40}\n Bot Settings\n{'─'*40}\n"
            f" CHECK_INTERVAL : {self.bot.check_interval} minute(s)\n"
            f" ADMIN_IDS      : {admins}\n"
            f" Servers        : {len(self.bot.servers)} configured\n"
            f"{'─'*40}\n Commands\n{'─'*40}\n"
            f" !addserver <id> [#ch]   Add server to monitor\n"
            f" !removeserver <id>      Remove server\n"
            f" !listservers            List all servers\n"
            f" !status [id]            Force update (all or one)\n"
            f" !setinterval <mins>     Poll interval (1-60)\n"
            f" !addadmin @user         Grant admin access\n"
            f" !removeadmin @user      Revoke admin access\n"
            f" !settings               Show this panel\n"
            f"{'─'*40}```"
        )
        await ctx.send(msg)


# ─── Bot ─────────────────────────────────────────────────────────────────────

class BattleMetricsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.check_interval = CHECK_INTERVAL
        self.servers: dict  = load_servers()   # { server_id: {channel_id, message_id} }
        self.player_state: dict = {}           # { server_id: set of player IDs }

    async def setup_hook(self):
        await self.add_cog(ConfigCog(self))
        self.monitor_loop.change_interval(minutes=self.check_interval)
        self.monitor_loop.start()

    async def on_ready(self):
        print(f'Logged in as {self.user}')
        print(f'Monitoring {len(self.servers)} server(s) every {self.check_interval} min')

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

    def format_server_message(self, data) -> str:
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
                    lines.append(f'[ {idx} | {pname} | {t} ]')
                except Exception as e:
                    print(f'Player parse error: {e}')

            body = '\n'.join(lines) if lines else 'No players online'
            return (
                f'```Server:  {name}\n'
                f'Status:  {status}\n'
                f'Players: {player_count}/{max_players}\n'
                f'Updated: {updated}\n\n'
                f'{body}```'
            )
        except Exception as e:
            print(f'Format error: {e}')
            return f'```Error formatting data: {e}```'

    # ── Update one server (fetch → edit or send) ──────────────────────────────

    async def update_server(self, server_id: str):
        """Fetch fresh data for one server and edit its status message."""
        cfg = self.servers.get(server_id)
        if not cfg:
            return

        data = await self.fetch_server_data(server_id)
        if not data:
            return

        # Detect player changes for console logging
        players = [
            item for item in data.get('included', [])
            if item.get('type') == 'player'
        ]
        current = {p.get('id') for p in players if p.get('id')}
        previous = self.player_state.get(server_id, current)
        joined = current - previous
        left   = previous - current
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

        content = self.format_server_message(data)

        # Try to edit the existing message; fall back to sending a new one
        if cfg.get('message_id'):
            try:
                msg = await channel.fetch_message(cfg['message_id'])
                await msg.edit(content=content)
                return
            except discord.NotFound:
                print(f'[{server_id}] Pinned message gone — will send a new one')
            except discord.Forbidden:
                print(f'[{server_id}] No permission to edit message')
                return

        # No message yet (or it was deleted) — send a fresh one and save its ID
        msg = await channel.send(content)
        self.servers[server_id]['message_id'] = msg.id
        save_servers(self.servers)

    # ── Monitor loop ─────────────────────────────────────────────────────────

    @tasks.loop(minutes=5)   # overridden in setup_hook
    async def monitor_loop(self):
        for server_id in list(self.servers.keys()):
            await self.update_server(server_id)

    @monitor_loop.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()
        print(f'Starting monitor loop for {len(self.servers)} server(s)...')
        # Seed player state without editing messages (they already exist from a prior run)
        for server_id in list(self.servers.keys()):
            data = await self.fetch_server_data(server_id)
            if data:
                players = [
                    p for p in data.get('included', [])
                    if p.get('type') == 'player'
                ]
                self.player_state[server_id] = {
                    p.get('id') for p in players if p.get('id')
                }
                # Also do an initial edit so the message is fresh after a restart
                await self.update_server(server_id)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    bot = BattleMetricsBot()
    bot.run(DISCORD_TOKEN)


if __name__ == '__main__':
    main()
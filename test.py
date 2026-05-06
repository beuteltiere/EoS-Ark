import discord
from discord.ext import tasks, commands
import aiohttp
import asyncio
from datetime import datetime
import os
from dotenv import load_dotenv
import json

# Load environment variables from .env file
load_dotenv()

# Configuration
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID'))  # Replace with your channel ID
SERVER_ID = os.getenv('SERVER_ID')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '1'))
BATTLEMETRICS_API = f'https://api.battlemetrics.com/servers/{SERVER_ID}'
NICKNAMES_FILE = 'nicknames.json'


class BattleMetricsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.previous_players = set()
        self.previous_data = None
        self.last_scheduled_update = None

    async def setup_hook(self):
        self.monitor_server.start()

    async def on_ready(self):
        print(f'Logged in as {self.user}')
        print(f'Monitoring server: {SERVER_ID}')

    def format_server_message(self, data):
        """Format the server status message"""
        try:
            from datetime import datetime, timezone

            attributes = data['data']['attributes']
            # Server info
            name = attributes.get('name', 'Unknown')
            player_count = attributes.get('players', 0)
            max_players = attributes.get('maxPlayers', 0)
            status = attributes.get('status', 'offline')

            # Get last query time
            details = attributes.get('details', {})
            last_query = details.get('time', 0)

            # Safely convert last_query to int
            try:
                last_query = int(last_query) if last_query else 0
            except (ValueError, TypeError):
                last_query = 0

            # Get players from included section
            included = data.get('included', [])
            players = [item for item in included if item.get('type') == 'player']

            # Build player list
            player_lines = []
            for idx, player in enumerate(players, 1):
                try:
                    player_attrs = player.get('attributes', {})
                    player_name = player_attrs.get('name', 'Unknown')
                    player_id = player_attrs.get('id', 'N/A')
                    
                    # Get time on server from meta.metadata
                    time_on_server = 0
                    meta = player.get('meta', {})
                    metadata = meta.get('metadata', [])
                    
                    # Find the time value in metadata array
                    for item in metadata:
                        if item.get('key') == 'time':
                            time_on_server = item.get('value', 0)
                            break
                    
                    # Convert seconds to hours and minutes
                    try:
                        time_seconds = int(time_on_server) if time_on_server else 0
                        hours = time_seconds // 3600
                        minutes = (time_seconds % 3600) // 60
                        
                        if hours > 0:
                            time_str = f"{hours}h {minutes}m"
                        else:
                            time_str = f"{minutes}m"
                    except (ValueError, TypeError):
                        time_str = "N/A"

                    player_line = f"[ {idx} | {player_name} | {time_str} ]"
                    player_lines.append(player_line)
                except Exception as e:
                    print(f"Error processing player {idx}: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

            # Combine all
            message = f"```Server: {name}\nStatus: {status}\nPlayers: {player_count}/{max_players}\n"
            if player_lines:
                message += "\n" + "\n".join(player_lines)
            else:
                message += "\nNo players online"
            message += "```"

            return message

        except Exception as e:
            print(f"Error formatting message: {e}")
            import traceback
            traceback.print_exc()
            return f"```Error formatting server data: {e}```"

    async def fetch_server_data(self):
        """Fetch server data from BattleMetrics API"""
        try:
            async with aiohttp.ClientSession() as session:
                # Get server info with players included
                url = f"{BATTLEMETRICS_API}?include=player,identifier"
                async with session.get(url) as response:
                    if response.status != 200:
                        print(f"API Error: {response.status}")
                        text = await response.text()
                        print(f"Response: {text[:500]}")
                        return None
                    return await response.json()
        except Exception as e:
            print(f"Error fetching data: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    @tasks.loop(minutes=CHECK_INTERVAL)
    async def monitor_server(self):
        """Monitor server every X minutes and check for player changes"""
        try:
            data = await self.fetch_server_data()
            
            if not data:
                return
            
            # Get current players from included section
            included = data.get('included', []) 
            players = [item for item in included if item.get('type') == 'player']
            current_players = {player.get('id') for player in players if player.get('id')}
            
            # Check if this is first run
            if self.previous_players is None:
                self.previous_players = current_players
                self.previous_data = data
                self.last_scheduled_update = datetime.now()
                return
            
            # Check for changes
            players_joined = current_players - self.previous_players
            players_left = self.previous_players - current_players
            
            # Determine if we should send an update
            should_send = False
            reason = ""
            
            # Check if players joined/left
            if players_joined or players_left:
                should_send = True
                if players_joined:
                    reason = f"{len(players_joined)} player(s) joined"
                if players_left:
                    if reason:
                        reason += f", {len(players_left)} player(s) left"
                    else:
                        reason = f"{len(players_left)} player(s) left"
            
            # Check if it's time for the scheduled 5-minute update
            current_time = datetime.now()
            if self.last_scheduled_update:
                time_since_last = (current_time - self.last_scheduled_update).total_seconds() / 60
                # Send scheduled update (always send every 5 minutes regardless of changes)
                if time_since_last >= CHECK_INTERVAL:
                    should_send = True
                    if not reason:
                        reason = "Scheduled 5-minute update"
                    self.last_scheduled_update = current_time
            else:
                self.last_scheduled_update = current_time
            
            # Send update if needed
            if should_send:
                channel = self.get_channel(CHANNEL_ID)
                if channel:
                    message = self.format_server_message(data)
                    await channel.send(message)
                    print(f"Update sent: {reason}")
            
            # Update stored data
            self.previous_players = current_players
            self.previous_data = data
            
        except Exception as e:
            print(f"Error in monitor loop: {e}")
            import traceback
            traceback.print_exc()

    @monitor_server.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()
        # Send initial status
        data = await self.fetch_server_data()
        if data:
            channel = self.get_channel(CHANNEL_ID)
            if channel:
                message = self.format_server_message(data)
                await channel.send(message)
                print("Initial status sent")

            # Initialize player tracking from included section
            included = data.get('included', [])
            players = [item for item in included if item.get('type') == 'player']
            self.previous_players = {player.get('id') for player in players if player.get('id')}
            self.previous_data = data
            self.last_scheduled_update = datetime.now()


def main():
    bot = BattleMetricsBot()
    bot.run(DISCORD_TOKEN)


if __name__ == '__main__':
    main()
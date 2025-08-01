import asyncio
import copy
import json
import random
from collections import deque
from dataclasses import dataclass, asdict
from pprint import pprint
from random import randint
import websockets
from string import ascii_letters, digits
from time import time
import argparse
import logging
import sys
import traceback

prnt = print


@dataclass
class Point:
    x: int
    y: int


@dataclass
class Snake:
    body: deque
    direction: str
    next_direction: str
    color: str
    name: str
    size: int = 0
    max_size: int = size
    alive: bool = True
    is_fast: str = False
    immortal: bool = True  # бессмерный

    def remove_segment(self, n=1, min_pop_size=2):
        for i in range(n):
            if len(self.body) > min_pop_size:
                self.body.pop()
                self.size = len(self.body)

    def add_segment(self, n=1):
        if n < 0:
            raise ValueError("Argument 'n' must be natural integer. ")
        for i in range(n):
            self.body.append(copy.copy(self.body[-1]))
        self.size = len(self.body)
        if len(self.body) > self.max_size:
            self.max_size = len(self.body)


@dataclass
class Player:
    player_id: str
    name: str
    color: str
    alive: bool
    deaths: int = 0
    kills: int = 0
    best_score: int = 0
    last_score: int = 0


class Server:
    VALID_NAME_CHARS = ascii_letters + digits + "_"

    DEFAULT_SNAKE_LENGHT = 1
    SNAKE_COLORS = ["red", "green", "blue", "yellow", "magenta", "cyan"]
    DIRECTIONS = ["right", "down", "left", "up"]

    # каждые 0.3 сек двигаемся

    def __init__(self, address, port, map_width=80, map_height=40, max_players=20, max_food=50,
                 server_name="Test Server", server_desc=None, logging_level="debug",
                 max_food_perc=10, normal_move_timeout=0.3):
        self.port = port
        self.address = address

        self.width = map_width
        self.height = map_height
        self.NORMAL_MOVE_TIMEOUT = normal_move_timeout
        self.FAST_MOVE_TIMEOUT = self.NORMAL_MOVE_TIMEOUT / 2
        self.snakes = {}
        self.food = []
        self.players = {}
        self.max_players = max_players
        self.connections = {}
        if server_desc is None:
            self.server_desc = f"<green>Welcome to our Server {server_name}!</green>"
        else:
            self.server_desc = server_desc

        self.game_speed = 0.002
        self.max_food_relative = max_food_perc / 100
        self.max_food = (self.width * self.height) * self.max_food_relative

        self.min_steling_snake_size = 5
        self.stealing_chance = 0.01
        self.steal_percentage = 0.1  # 5%

        self.old_tick_time = time()
        self.tick = 0.02  # sec

        self.last_normal_snake_move_time = time()
        self.last_fast_snake_move_time = time()

        self.logging_level = logging_level
        self.setup_logger(__name__, "../server.log", getattr(logging, self.logging_level))
        self.logger.info(f"Logging level: {self.logging_level}")

    async def set_server_desc(self, server_desc):
        self.server_desc = server_desc
        await self.broadcast_chat_message({"type": "set_server_desc",
                                           "data": self.server_desc})

    def setup_logger(self, name, log_file='server.log', level=logging.INFO):
        """Настройка логгера с выводом в консоль и файл."""
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)

        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_formatter = logging.Formatter('[%(levelname)s] %(message)s')

        # Обработчик для записи в файл
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(file_formatter)

        # Обработчик для вывода в консоль
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)

        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        return self.logger

    def get_all_food_count(self):
        food_count = 0
        food_count += len(self.food)

        for k, v in self.snakes.items():
            food_count += len(v.body)
        return food_count

    def get_avalible_coords(self):
        x1, y1, x2, y2 = self.get_map_rect()
        while True:
            x = random.randint(x1, x2)
            y = random.randint(y1, y2)
            p = (x, y)
            if not p in self.food:
                break
        return x, y

    def get_addres_from_ws(self, ws):
        return ":".join(str(i) for i in ws.remote_address)

    async def add_player(self, player_id: str, name, color):
        if player_id in self.snakes:
            return False
        self.players[player_id] = Player(
            player_id=player_id,
            name=name,
            color=color,
            alive=True)

        await self.spawn(player_id)
        await self.broadcast_chat_message({"type": "chat_message", "subtype": "join/left",
                                           "data": f"<yellow>[</yellow><green>+</green><yellow>]</yellow> {await self.get_stilizate_name_color(player_id)} <yellow>joined the game</yellow>"})
        self.logger.info(
            f"Connection {self.get_addres_from_ws(self.connections[player_id])} registred as {self.get_player(player_id)}")
        return True

    async def remove_player(self, player_id):
        if player_id not in self.players.keys():
            return True
        del self.connections[player_id]
        self.logger.info(f"Player {self.get_player(player_id)} disconnected")
        await self.broadcast_chat_message({"type": "chat_message", "subtype": "join/left",
                                           "data": f"<yellow>[</yellow><red>-</red><yellow>]</yellow> {await self.get_stilizate_name_color(player_id)} <yellow>left the game</yellow>"})

        if player_id in self.snakes:
            del self.snakes[player_id]
        if player_id in self.players:
            del self.players[player_id]
        return True

    def change_direction(self, player_id, direction):
        if player_id in self.snakes:
            snake = self.snakes[player_id]
            # Prevent 180-degree turns
            if (direction == 'up' and snake.direction != 'down') or \
                    (direction == 'down' and snake.direction != 'up') or \
                    (direction == 'left' and snake.direction != 'right') or \
                    (direction == 'right' and snake.direction != 'left'):
                snake.next_direction = direction

    def generate_food(self):
        if self.get_all_food_count() < self.max_food:
            x1, y1, x2, y2 = self.get_map_rect()
            if len(self.food) < self.max_food:
                x = random.randint(x1, x2)
                y = random.randint(y1, y2)
                self.food.append(Point(x, y))

    def get_player(self, player_id):
        return f"@{self.players[player_id].name}#{player_id}"

    async def player_death(self, player_id, reason: str = "No reason", if_immortal=False):
        if self.snakes[player_id].immortal and not if_immortal:
            return False

        self.snakes[player_id].alive = False
        body = self.snakes[player_id].body
        # del self.snakes[player_id]
        self.players[player_id].alive = False
        self.players[player_id].deaths += 1
        state = self.to_dict()
        ws = self.connections[player_id]
        await ws.send(json.dumps(state))
        text = f'{reason.replace("%NAME%", await self.get_stilizate_name_color(player_id))}'
        self.logger.info(f"Player {self.get_player(player_id)} death ({text})")
        await self.connections[player_id].send(json.dumps({"type": "you_died", "data": text}))
        await self.broadcast_chat_message({"type": "chat_message", "subtype": "death_message",
                                           "data": text})
        for i in body:
            self.food.append(i)

        return True

    def get_map_rect(self):
        x1, y1, x2, y2 = -(self.width // 2), -(self.height // 2), self.width // 2, self.height // 2
        return x1, y1, x2, y2

    def is_name_valid(self, name: str):
        if len(name) > 16:
            return f'Nickname "{name}" is too long'
        elif len(name) < 4:
            return f'Nickname "{name}" is too short'

        for i in name:
            if i.lower() not in self.VALID_NAME_CHARS:
                return f'Nickname "{name}" contain invalid characters'

        return True

    async def update(self):
        self.generate_food()

        # Update directions for all snakes first
        for snake in self.snakes.values():
            snake.direction = snake.next_direction

        # Determine movement timing
        now = time()
        move_normal = now >= self.last_normal_snake_move_time + self.NORMAL_MOVE_TIMEOUT
        move_fast = now >= self.last_fast_snake_move_time + self.FAST_MOVE_TIMEOUT

        if not (move_normal or move_fast):
            return

        # Update movement timers
        if move_normal:
            self.last_normal_snake_move_time = now
        if move_fast:
            self.last_fast_snake_move_time = now

        # Process movement for each snake
        for player_id, snake in list(self.snakes.items()):
            if not snake.alive:
                continue

            # Check if this snake should move now
            should_move = (snake.is_fast and move_fast) or (not snake.is_fast and move_normal)
            if not should_move:
                continue

            # Calculate new head position
            head = snake.body[0]
            new_head = Point(head.x, head.y)

            direction_offsets = {
                'up': (0, -1),
                'down': (0, 1),
                'left': (-1, 0),
                'right': (1, 0)
            }
            dx, dy = direction_offsets[snake.direction]
            new_head.x += dx
            new_head.y += dy

            # Check wall collision
            walls = self.get_map_rect()
            if not (walls[0] <= new_head.x <= walls[2] and walls[1] <= new_head.y <= walls[3]):
                await self.player_death(player_id, "Crashed into the border")
                continue

            # Check snake collisions
            for other_id, other_snake in self.snakes.items():
                if other_id == player_id:
                    continue
                if other_snake.alive and new_head in other_snake.body:
                    await self.player_death(player_id, f'Crashed into {other_snake.name}')
                    self.players[other_id].kills += 1
                    break
            else:  # No collision occurred
                # Check food collision
                for i, food in enumerate(self.food):
                    if new_head.x == food.x and new_head.y == food.y:
                        self.food.pop(i)
                        snake.add_segment()
                        break

                # Move snake
                snake.body.appendleft(new_head)
                snake.remove_segment()

    def to_dict(self):
        dict_ = {
            'type': "game_state",
            'map_borders': [i for i in self.get_map_rect()],
            "snakes": {},
            "players": {},
            "food": []}

        for pid, s in self.snakes.items():
            dict_["snakes"][pid] = {
                'body': [asdict(p) for p in s.body],
                'color': s.color,
                'name': s.name,
                'size': s.size,
                'max_size': s.max_size,
                'alive': s.alive,
                'direction': s.direction,
            }

        for f in self.food:
            dict_['food'].append(asdict(f))

        for pid, pl in self.players.items():
            sn = self.snakes.get(pid, None)
            dict_['players'][pid] = {"name": pl.name,
                                     "color": pl.color,
                                     "alive": pl.alive,
                                     "kills": pl.kills,
                                     "deaths": pl.deaths
                                     }

        return dict_

    async def broadcast_chat_message(self, data):
        connections_ = copy.copy(self.connections)
        to_send = json.dumps(data)
        self.logger.debug(f"Broadcast data: {data}")

        for player_id, ws in connections_.items():


            await ws.send(to_send)


    async def get_stilizate_name_color(self, player_id, text=None):

        color = self.players.get(player_id, {}).color
        if text == None:
            text = self.players.get(player_id).name

        if color in self.SNAKE_COLORS:
            pass
        else:
            color = "white"

        return f"<{color}>{text}</{color}>"

    async def handle_client_chat_message(self, player_id, message: str):
        con = self.connections[player_id]
        if message.startswith("/"):
            lst = message.split()
            if lst[0] == "/help":
                await con.send(json.dumps({"type": "chat_message",
                                           "data": f"Help mesaage here?"}))
            elif lst[0] == "/kill":
                await self.player_death(player_id, "%NAME% used /kill command", if_immortal=True)
        else:
            name = self.players[player_id].name
            await self.broadcast_chat_message(
                {"type": "chat_message", "data": f"{message}",
                 "from_user": f"{await self.get_stilizate_name_color(player_id)}"})

    async def handle_client_data(self, player_id: str, data: dict):
        self.logger.debug(f"Recieved data from {self.get_player(player_id)}: {data}")
        if data["type"] == "direction":
            self.change_direction(player_id, data['data'])
        elif data["type"] == "chat_message":
            await self.handle_client_chat_message(player_id, data["data"])
        elif data["type"] == "kill_me":
            await self.player_death(player_id)
        elif data["type"] == "respawn":
            await self.respawn(player_id)

    async def spawn(self, player_id, lenght=DEFAULT_SNAKE_LENGHT):
        x, y = self.get_avalible_coords()

        body = deque([Point(x, y)])
        sn = self.snakes[player_id] = Snake(
            body=body,
            direction='right',
            next_direction='right',
            color=self.players[player_id].color,
            name=self.players[player_id].name,
            alive=True,

        )

        sn.add_segment(lenght )
        self.logger.info(f"Spawned {self.get_player(player_id)} ({self.players[player_id].name})")


        # self.players[player_id].alive = True

    async def respawn(self, player_id):

        await self.spawn(player_id)

    async def handle_connection(self, websocket):

        if len(self.players) >= self.max_players:
            self.logger.info(f"{websocket.remote_address} is trying to connect, but the server is full")
            await websocket.send(json.dumps({"type": "connection_error",
                                             "data": f"Server is full ({len(self.players)} / {self.max_players})"}))
            return
        else:
            self.logger.debug(f"{websocket.remote_address} is trying to connect to the server")
        while True:
            player_id = get_random_id()
            if not (player_id in self.players.keys()):
                self.logger.debug(f"{websocket.remote_address}`s player_id={player_id}")
                break
        self.connections[player_id] = websocket
        await websocket.send(json.dumps({"player_id": player_id, "type": "player_id"}))
        try:
            data = await websocket.recv()
            try:
                player_info = json.loads(data)
                name = player_info.get('name', 'Player')
                name_valid = self.is_name_valid(name)
                if not name_valid is True:
                    self.logger.debug(f"{websocket.remote_address} choosen invalid name")
                    await websocket.send(json.dumps({"type": "connection_error",
                                                     "data": f"Invalid name: {name_valid}"}))
                    return

                color = player_info.get('color', 'green')
                if not color in self.SNAKE_COLORS:
                    self.logger.debug(f"{websocket.remote_address} choosen invalid color")
                    await websocket.send(json.dumps({"type": "connection_error",
                                                     "data": f"Invalid snake color\nValid colors: {', '.join(self.SNAKE_COLORS)}"}))
                    return

                await self.add_player(player_id, name, color)
                await websocket.send(json.dumps({"type": "set_server_desc", "data": self.server_desc}))
                await websocket.send(json.dumps(self.to_dict()))
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        await self.handle_client_data(player_id, data)
                    except:
                        pass

            except (json.JSONDecodeError, websockets.exceptions.ConnectionClosedError) as e:
                self.logger.warning(f"{type(e).__name__}: {e}")
                await websocket.close()
                return


        finally:

            await websocket.close()
            await self.remove_player(player_id)

    async def steal_body(self, player_id):

        snake = self.snakes[player_id]
        if not snake.alive:
            return
        if random.random() < self.stealing_chance:
            current_length = len(snake.body)
            if current_length > self.min_steling_snake_size:
                segments_to_remove = max(1, int(current_length * self.steal_percentage))
                self.logger.debug(
                    f"Stole {segments_to_remove} segments ({self.steal_percentage * 100}%) from {self.get_player(player_id)}")

                snake.remove_segment(segments_to_remove, min_pop_size=self.min_steling_snake_size)

    async def on_tick(self):
        for player_id, pl in self.players.items():
            pass
            await self.steal_body(player_id)

        await self.send_game_state_to_all()

    async def send_game_state_to_all(self):
        state = self.to_dict()

        connections_ = copy.copy(self.connections)
        for player_id, ws in connections_.items():
            try:
                await ws.send(json.dumps(state))
            except websockets.exceptions.ConnectionClosedOK:
                pass
            finally:
                pass

    async def game_loop(self):

        while True:
            await self.update()
            now = time()
            if now >= self.old_tick_time + self.tick:
                self.old_tick_time = now
                await self.on_tick()

            await asyncio.sleep(self.game_speed)

    async def run(self):
        self.game_task = asyncio.create_task(self.game_loop())
        try:
            async with websockets.serve(self.handle_connection, self.address, self.port):
                print(f"Server started at {self.address}:{self.port}")
                await asyncio.Future()
        except asyncio.CancelledError:
            pass

        except Exception as e:
            self.logger.critical(traceback.format_exc())
            self.logger.critical(f"Crashed. Error: {type(e).__name__}: {e}")

        finally:
            self.game_task.cancel()
            try:
                await self.game_task
            except KeyboardInterrupt:
                pass


def get_random_id():
    return hex(random.randint(0, 131_072))


def positive_int(value):
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive integer")
    return ivalue


async def run_server():
    parser = argparse.ArgumentParser(description="Multiplayer Snake game by @Arizel79 (server)")
    parser.add_argument('--address', "--ip", type=str, help='Server port (default: 8090)', default="0.0.0.0")
    parser.add_argument('--port', "--p", type=int, help='Server port (default: 8090)', default=8090)
    parser.add_argument('--server_name', type=str, help='Server name', default="Snake Server")
    parser.add_argument('--server_desc', type=str, help='Description of server', default=None)
    parser.add_argument('--max_players', type=positive_int, help='Max online players count', default=20)
    parser.add_argument('--map_width', "--width", "--w","--x_size", type=int, help='Width of server map', default=120)
    parser.add_argument('--map_height',"--height", "--h","--y_size", type=int, help='Height of server map', default=60)
    parser.add_argument('--food_perc', type=int, help='Proportion food/map in %%', default=3)
    parser.add_argument('--move_timeout', type=int, help='Timeout move snake', default=0.1)
    parser.add_argument('--log_lvl', type=str, choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        help='Level of logging', default="INFO")
    args = parser.parse_args()

    game_state = Server(address=args.address, port=args.port, map_width=args.map_width, map_height=args.map_height,
                        max_players=args.max_players, server_name=args.server_name, server_desc=args.server_desc,
                        max_food_perc=args.food_perc, logging_level=args.log_lvl, normal_move_timeout=args.move_timeout)
    try:
        await game_state.run()
    except asyncio.CancelledError:
        pass  # Игнорируем CancelledError при нормальном завершении
    finally:
        pass


def main():
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        print("\nKeyboardInterrupt. Server quit")
        return


if __name__ == '__main__':
    main()

import json
import base64
import hashlib
import secrets
import pathlib
import platform
from typing import Any, Dict

from StreamDeck.Devices.StreamDeckMini import StreamDeckMini
from StreamDeck.Devices.StreamDeckOriginal import StreamDeckOriginal
from StreamDeck.Devices.StreamDeckOriginalV2 import StreamDeckOriginalV2
from StreamDeck.Devices.StreamDeckXL import StreamDeckXL

from PySide2.QtCore import QObject, Signal
import PySide2.QtNetwork
from PySide2.QtWebSockets import QWebSocketServer, QWebSocket

from streamdeck_ui import api, __version__
from streamdeck_ui.config import CACHE_DIRECTORY

class StreamDeckWsServer(QObject):
    client_connected = Signal(QWebSocket)
    client_disconnected = Signal(QWebSocket)
    client_message_received = Signal(QWebSocket, dict)

    def __init__(self, port: int, parent=None) -> None:
        super().__init__(parent)
        #self.host = PySide2.QtNetwork.QHostAddress.LocalHost
        self.host = PySide2.QtNetwork.QHostAddress.AnyIPv4
        self.port = port
        self.clients = []
        self.server: QWebSocketServer = QWebSocketServer(
            parent.serverName(),
            parent.secureMode(),
            parent=parent)
        if self.server.listen(self.host, port):
            print(f'Websocket Server Listening: ws://{self.server.serverAddress().toString()}:{self.server.serverPort()}')
        self.server.acceptError.connect(self.on_accept_error)
        self.server.newConnection.connect(self.on_new_connection)
        self.client_connection = None

        print(self.server.isListening())


    def on_accept_error(self, accept_error):
        print(f'Error: {accept_error}')


    def on_new_connection(self):
        self.client_connection = self.server.nextPendingConnection()
        self.client_connection.textMessageReceived.connect(self.process_text_message)
        #self.client_connection.textFrameReceived.connect(self.process_text_frame)
        #self.client_connection.binaryMessageReceived.connect(self.process_binary_message)
        self.client_connection.disconnected.connect(self.socket_disconnected)

        print('New client')
        self.clients.append(self.client_connection)
        self.client_connected.emit(self.client_connection)


    def process_text_message(self, message):
        print(f'm:{message}')
        #if self.client_connection:
        #    for client in self.clients:
        #        client.sendTextMessage(message)
        try:
            message_dict = json.loads(message)
        except Exception as e:
            print(f'Error parsing client message: {e}')
            if self.client_connection:
                self.client_connection.sendTextMessage(json.dumps({
                    'event': 'error',
                    'payload': {
                        'message': str(e)
                    }
                }))
            return
        self.client_message_received.emit(self.client_connection, message_dict)


    def process_text_frame(self, frame, is_last_frame):
        print(f'f:{frame}/{is_last_frame}')


    def process_binary_message(self, message):
        print(f'b:{message}')
        if self.client_connection:
            self.client_connection.sendBinaryMessage(message)


    def socket_disconnected(self):
        print('Socket disconnected')
        if self.client_connection:
            self.clients.remove(self.client_connection)
            self.client_disconnected.emit(self.client_connection)
            self.client_connection.deleteLater()
            self.client_connection = None


class PluginServer(QObject):
    def __init__(self, ws_server: 'StreamDeckWsServer', device_pixel_ratio: float, parent: QObject=None) -> None:
        super().__init__(parent)
        self.ws_server = ws_server
        self.device_pixel_ratio = device_pixel_ratio
        self.plugin_to_client_map: Dict[str, str] = {}
        self.ws_server.client_connected.connect(self.on_client_connected)
        self.ws_server.client_disconnected.connect(self.on_client_disconnected)
        self.ws_server.client_message_received.connect(self.receive_event)

    # per https://developer.elgato.com/documentation/stream-deck/sdk/registration-procedure/#Info-parameter
    DEVICE_MAP = {
        StreamDeckOriginal.DECK_TYPE: 0, # kESDSDKDeviceType_StreamDeck
        StreamDeckOriginalV2.DECK_TYPE: 0, # kESDSDKDeviceType_StreamDeck
        StreamDeckMini.DECK_TYPE: 1, # kESDSDKDeviceType_StreamDeckMini
        StreamDeckXL.DECK_TYPE: 2, # kESDSDKDeviceType_StreamDeckXL
        # This app does not support StreamDeckMobile or Corsair GKeys
        # kESDSDKDeviceType_StreamDeckMobile (3)
        # kESDSDKDeviceType_CorsairGKeys (4)
    }

    REGISTER_EVENT = 'registerPlugin'


    def __get_plugin_id(self, requesting_client:QWebSocket):
        for plugin_id, client in list(self.plugin_to_client_map.items()):
            if requesting_client == client:
                return plugin_id
        return None

    def __key_id_from_column_row(self, deck_id, column: int, row: int):
        device_cols = api.decks[deck_id].KEY_COLS
        return (device_cols * row) + column
        

    def __column_row_from_key_id(self, deck_id, key: int):
        device_cols = api.decks[deck_id].KEY_COLS
        return (key % device_cols, key // device_cols)


    def cache_image(self, encoded_image_data: str) -> pathlib.Path:
        cache_dir = pathlib.Path(CACHE_DIRECTORY) / 'icons'
        if not cache_dir.exists():
            cache_dir.mkdir(parents=True)
        hash = hashlib.blake2b(digest_size=16)
        hash.update(encoded_image_data.encode('utf-8'))
        fname = hash.hexdigest()
        info, _, content = encoded_image_data.partition(';')
        _, _, mime_type = info.partition(':')
        if mime_type is None:
            return None
        mime_type = mime_type.strip()
        # per https://developer.elgato.com/documentation/stream-deck/sdk/events-sent/#setimage
        if mime_type == 'image/png':
            extension = '.png'
        elif mime_type == 'image/jpg':
            extension = '.jpg'
        elif mime_type == 'image/bmp':
            extension = '.bmp'
        elif mime_type == 'image/svg+xml':
            extension = '.svg'
        else:
            # not supported mime type
            return None
        image_file = cache_dir / (fname + extension)
        if not image_file.exists():
            header, _, data = content.partition(',')
            if mime_type == 'image/svg+xml':
                with open(image_file, 'w', encoding='utf-8') as f:
                    f.write(data)
            else:
                with open(image_file, 'wb') as f:
                    result = base64.b64decode(data)
                    f.write(result)
        return image_file



    def receive_event(self, client: QWebSocket, evt: Dict[str, Any]):
        event_name = evt.get('event')
        if event_name == self.REGISTER_EVENT:
            plugin_id = evt.get('uuid')  # TODO verify that this uuid is one we've previously sent to a connected client
            if plugin_id is not None:
                self.plugin_to_client_map[plugin_id] = client
                print(f'Plugin {plugin_id} has registered!')
            return

        plugin_id = self.__get_plugin_id(client)
        if plugin_id is None:
            return  # don't process events from unregistered clients
        payload = evt.get('payload', {})
        try:
            if event_name == 'setPage':
                # {'event': 'setPage', 'payload': {'device': 'device_id', 'page': 0}
                device_id = payload.get('device')
                page_num = payload.get('page')
                if page_num is not None and page_num >= 0 and device_id in api.decks:
                    api.set_page(device_id, page_num)
            elif event_name == 'setImage':
                # {'event': 'setImage', 'payload': {'device': 'device_id', 'page': 0, 'row': 0, 'column': 0, 'image': <base64 encoded image> }}
                # "data:image/png;base64,iVBORw0KGgoA..."
                # "data:image/jpg;base64,/9j/4AAQSkZJ..."
                # "data:image/bmp;base64,/9j/Qk32PAAA..."
                device_id = payload.get('device')
                page = payload.get('page')
                row = payload.get('row')
                column = payload.get('column')
                key = self.__key_id_from_column_row(device_id, column, row)
                encoded_image = payload.get('image')
                if not encoded_image:
                    api.set_button_icon(device_id, page, key, '')
                    return
                image_path = self.cache_image(encoded_image)
                api.set_button_icon(device_id, page, key, str(image_path))
            elif event_name == 'setTitle':
                # {'event': 'setTitle', 'payload': {'device': 'device_id', 'page': 0, 'row': 0, 'column': 0, 'title': 'text'}}
                device_id = payload.get('device')
                page = payload.get('page')
                if page is None:
                    print('No page provided')
                    return
                row = payload.get('row')
                if row is None:
                    print('No row provided')
                    return
                column = payload.get('column')
                if column is None:
                    print('No column provided')
                    return
                key = self.__key_id_from_column_row(device_id, column, row)
                text = payload.get('title')
                if text is None:
                    text = ''
                api.set_button_text(device_id, page, key, text)


        except Exception as e:
            print(e)



    def send_event(self, event_data, plugin_id=None):
        if plugin_id is not None:
            client = self.plugin_to_client_map.get(plugin_id)
            if client is not None:  # Sending to just one client
                clients = [client]
            else:  # Single client requested, but it's no longer connected
                return
        else:  # Send to all clients
            clients = list(self.plugin_to_client_map.values())
            
        event = json.dumps(event_data, indent=2)
        for client in clients:
            client.sendTextMessage(event)


    def handle_keypress(self, deck_id: str, key: int, state: bool) -> None:
        deck = api.decks[deck_id]
        col, row = self.__column_row_from_key_id(deck_id, key)
        event_data = {
            'event': 'keyDown' if state else 'keyUp',
            'deck_id': deck_id,
            'deck': deck.DECK_TYPE,
            'key': key,
            'state': state,
            'payload': {
                'page': api.get_page(deck_id),
                'coordinates': {
                    'column': col,
                    'row': row
                }
            }
        }
        self.send_event(event_data)


    def on_client_connected(self, client: QWebSocket):
        plugin_id = secrets.token_hex(16)
        devices = []
        for deck_id, deck in api.decks.items():
            devices.append({
                'id': deck_id,
                'size': {
                    'columns': deck.KEY_COLS,
                    'rows': deck.KEY_ROWS
                },
                'type': deck.DECK_TYPE,
                'current_page': api.get_page(deck_id)
            })
        event = {
            'event': 'connectElgatoStreamDeckSocket',
            'payload': {
                'inPort': self.ws_server.port,
                'inPluginUUID': plugin_id,
                'inRegisterEvent': self.REGISTER_EVENT,
                'inInfo': {
                    'application': {
                        'language': 'en',
                        'platform': 'kESDSDKApplicationInfoPlatformLinux',  # TODO emulate Windows/Mac
                        'version': __version__,
                        'platformVersion': platform.version()
                    },
                    'plugin': {
                        'uuid': plugin_id,
                        'version': '1.0' # TODO read from a manifest
                    },
                    'devicePixelRatio': int(self.device_pixel_ratio),
                    'colors': {  # rgba TODO configure this?
                        'actionImageColor': '#D8D8D8FF',
                        'categoryIconColor': '#C8C8C8FF',
                        'keyImageColor': '#D8D8D8FF',
                        'keyImagePressedColor': '#969696FF',
                        'backgroundColor': '#1D1E1FF',
                        'buttonPressedBackgroundColor': '#303030FF', 
                        'buttonPressedBorderColor': '#646464FF', 
                        'buttonPressedTextColor': '#969696FF', 
                        'disabledColor': '#F7821B59', 
                        'highlightColor': '#F7821BFF', 
                        'mouseDownColor': '#CF6304FF'
                    },
                    'devices': devices
                }
            }
        }
        # we can't use self.send_event here because
        # the client hasn't yet registered
        event_str = json.dumps(event, indent=2)
        client.sendTextMessage(event_str)

    def on_client_disconnected(self, client: QWebSocket):
        for plugin_id in list(self.plugin_to_client_map.keys()):
            if self.plugin_to_client_map[plugin_id] == client:
                print(f'Removing plugin {plugin_id} because it has disconnected')
                del self.plugin_to_client_map[plugin_id]


def start_server(port: int, parent=None):
    server_obj = QWebSocketServer(
        'streamdeck-ui',
        QWebSocketServer.SslMode.NonSecureMode,
        parent=parent)
    server_obj.closed.connect(lambda: print('Closed'))
    return StreamDeckWsServer(port, server_obj)

if __name__ == '__main__':
    import sys

    from PySide2 import QtCore, QtWebSockets, QtNetwork
    from PySide2.QtCore import QUrl, QCoreApplication, QTimer
    from PySide2.QtWidgets import QApplication


    class Client(QtCore.QObject):
        def __init__(self, parent):
            super().__init__(parent)

            self.client =  QtWebSockets.QWebSocket("",QtWebSockets.QWebSocketProtocol.Version13,None)
            self.client.error.connect(self.error)

            self.client.open(QUrl("ws://127.0.0.1:1302"))
            self.client.pong.connect(self.onPong)

        def do_ping(self):
            print("client: do_ping")
            self.client.ping(b"foo")

        def send_message(self):
            print("client: send_message")
            self.client.sendTextMessage("asd")

        def onPong(self, elapsedTime, payload):
            print("onPong - time: {} ; payload: {}".format(elapsedTime, payload))

        def error(self, error_code):
            print("error code: {}".format(error_code))
            print(self.client.errorString())

        def close(self):
            self.client.close()

    def quit_app():
        print("timer timeout - exiting")
        QCoreApplication.quit()

    def ping():
        client.do_ping()

    def send_message():
        client.send_message()

    if __name__ == '__main__':
        global client
        app = QApplication(sys.argv)

        QTimer.singleShot(2000, ping)
        QTimer.singleShot(3000, send_message)
        QTimer.singleShot(5000, quit_app)

        client = Client(app)

        app.exec_()
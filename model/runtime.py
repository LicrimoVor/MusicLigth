import asyncio
import threading


def start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def create_async_loop():
    loop = asyncio.new_event_loop()
    threading.Thread(target=start_loop, args=(loop,), daemon=True).start()
    return loop

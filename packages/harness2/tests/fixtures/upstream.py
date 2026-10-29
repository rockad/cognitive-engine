"""A throwaway 'remote' endpoint for the tests: answers any request on 9000 and 9001, logs only the port."""
import asyncio


async def handle(port, reader, writer):
    print(f"CONN {port}", flush=True)
    try:
        await asyncio.wait_for(reader.read(4096), 1.0)
    except asyncio.TimeoutError:
        pass
    body = f"UPSTREAM-OK-{port}".encode()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
    await writer.drain()
    writer.close()


async def main():
    servers = [await asyncio.start_server(lambda r, w, p=p: handle(p, r, w), "0.0.0.0", p) for p in (9000, 9001)]
    print("listening", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))

asyncio.run(main())

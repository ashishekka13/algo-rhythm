"""The Cloudflare Worker's headers (deploy/cloudflare/worker.js) are believed only with the shared EDGE_SECRET."""
import os

from starlette.requests import Request

from algo_rhythm import server


def req(**headers):
    raw = [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "query_string": b"",
                    "server": ("svc.a.run.app", 443), "scheme": "https"})


def main():
    os.environ.pop("EDGE_SECRET", None)
    worker = {"host": "svc.a.run.app", "x_forwarded_host": "algo-rhythm.in", "x_edge_secret": "s3cret"}
    assert not server.from_edge(req(**worker)), "no secret set on the service: never believed"
    assert server.public_host(req(**worker)) == "svc.a.run.app"
    os.environ["EDGE_SECRET"] = "s3cret"
    assert server.from_edge(req(**worker)) and server.public_host(req(**worker)) == "algo-rhythm.in"
    forged = dict(worker, x_edge_secret="guess")
    assert not server.from_edge(req(**forged)) and server.public_host(req(**forged)) == "svc.a.run.app"
    assert not server.from_edge(req(host="svc.a.run.app")), "straight to run.app: no secret, no trust"
    os.environ.pop("EDGE_SECRET")
    print("EDGE PASS")


if __name__ == "__main__":
    main()

"""Read-only liveness/readiness probe. Run on your host's monitoring schedule.

Prints status metadata only, never response bodies or URL credentials. This
command does not install a scheduler, send alerts or write financial data.
"""
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, target):
        return None


def check_service(base_url, timeout=5):
    url = urllib.parse.urlsplit(base_url)
    if (url.username or url.password or url.query or url.fragment or url.path not in ('', '/')
            or not url.hostname or url.scheme not in ('http', 'https')
            or (url.scheme == 'http' and url.hostname not in ('127.0.0.1','localhost','::1'))):
        raise ValueError('Use an HTTPS origin or a local HTTP origin, without credentials, a path or a query.')
    results = {}
    opener = urllib.request.build_opener(NoRedirect)
    for label, path in [('liveness','/'),('readiness','/ready')]:
        try:
            with opener.open(urllib.request.Request(base_url.rstrip('/')+path,
                    headers={'Accept':'application/json','User-Agent':'Finio-health-check'}), timeout=timeout) as response:
                results[label] = response.status
        except urllib.error.HTTPError as error:
            results[label] = error.code
        except (urllib.error.URLError, TimeoutError, OSError):
            results[label] = 'unreachable'
    return {'healthy':all(value==200 for value in results.values()),'checks':results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    args = parser.parse_args()
    try:
        result = check_service(args.base_url)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result))
    return 0 if result['healthy'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

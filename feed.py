"""Feeder client: captures from a webcam and POSTs JPEG frames to the server's /CCTV/push_frame endpoint.

Usage:
    python feed.py --url http://yourserver:8000 --key <FEED_KEY> --camera 0 --fps 5

This script runs on your feeder laptop (not the server). It retries on network errors.
"""
import time
import argparse
import requests
import cv2


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', required=True, help='Base URL of the server (e.g. http://example.com:8000)')
    p.add_argument('--key', required=True, help='Feed key (must match CCTV_FEED_KEY on server)')
    p.add_argument('--id', required=False, help='Camera id to register this feed as', default=None)
    p.add_argument('--camera', default=0, help='Camera index or device path',)
    p.add_argument('--fps', type=float, default=5.0, help='Frames per second to push')
    p.add_argument('--quality', type=int, default=80, help='JPEG quality 1-100')
    args = p.parse_args()

    push_url = args.url.rstrip('/') + '/CCTV/push_frame'
    cap = cv2.VideoCapture(int(args.camera) if str(args.camera).isdigit() else args.camera)
    if not cap.isOpened():
        print('Unable to open camera', args.camera)
        return

    interval = 1.0 / max(0.1, args.fps)
    print('Starting feeder to', push_url, 'fps=', args.fps)

    while True:
        start = time.time()
        ret, frame = cap.read()
        if not ret:
            print('frame read failed, retrying...')
            time.sleep(1.0)
            continue

        # encode JPEG
        ret2, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), args.quality])
        if not ret2:
            print('jpeg encode failed')
            time.sleep(1.0)
            continue

        try:
            headers = {'X-Feed-Key': args.key, 'Content-Type': 'image/jpeg'}
            if args.id:
                headers['X-Cam-ID'] = args.id
            r = requests.post(push_url, headers=headers, data=jpeg.tobytes(), timeout=5)
            if r.status_code not in (200, 204):
                print('push failed', r.status_code, r.text)
        except Exception as e:
            print('push exception', e)

        # sleep to maintain fps
        elapsed = time.time() - start
        to_sleep = interval - elapsed
        if to_sleep > 0:
            time.sleep(to_sleep)


if __name__ == '__main__':
    main()

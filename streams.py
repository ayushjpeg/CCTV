import os
import cv2
import sys

class Camera:
    def __init__(self, camera_index='0'):
        """camera_index can be an int index (0,1..) or a device path like /dev/video0 or an RTSP URL"""
        # try to interpret as int
        try:
            idx = int(camera_index)
        except Exception:
            idx = camera_index

        # create VideoCapture
        self.cap = cv2.VideoCapture(idx)
        if not self.cap.isOpened():
            # try with string path explicitly
            self.cap.open(str(camera_index))

    def get_frame(self):
        if not self.cap or not self.cap.isOpened():
            return None
        ret, frame = self.cap.read()
        if not ret:
            return None
        # encode as jpeg
        ret2, jpeg = cv2.imencode('.jpg', frame)
        if not ret2:
            return None
        return jpeg.tobytes()

    def __del__(self):
        try:
            if self.cap and self.cap.isOpened():
                self.cap.release()
        except Exception:
            pass

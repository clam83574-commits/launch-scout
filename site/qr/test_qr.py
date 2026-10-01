import cv2, sys
d = cv2.QRCodeDetector()
img = cv2.imread(sys.argv[1])
ok = []
for fx in (1.0, 0.6, 0.35, 0.25):
    im = cv2.resize(img, None, fx=fx, fy=fx, interpolation=cv2.INTER_AREA) if fx != 1 else img
    t, _, _ = d.detectAndDecode(im)
    ok.append(bool(t))
print(sys.argv[2], ok)

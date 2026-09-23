"""
finish-line camera application package.

Modules:
    config            - all tunable settings
    camera_manager    - real (Picamera2) or mock camera, frame supply
    motion_detector   - NumPy frame-differencing within a redefinable ROI
    bib_recognizer    - pluggable OCR backends (Tesseract / EasyOCR / none)
    database          - SQLite storage of captures + detections
    capture_service   - motion -> capture -> OCR -> store pipeline
    gpio_controller   - stubbed GPIO hooks for future hardware
    web_app           - Flask three-pane dashboard
"""

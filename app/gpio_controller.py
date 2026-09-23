"""
gpio_controller.py
=================

A deliberately thin, *future-proof* wrapper around the Raspberry Pi GPIO pins.

Right now GPIO is disabled (config.GPIO_ENABLED is False) and every method is a
no-op, so the rest of the program can call these hooks unconditionally without
caring whether any hardware exists yet.  When you are ready to add physical
indicators or triggers, you only have to:

    1. Set GPIO_ENABLED = True and fill in the pin numbers in config.py.
    2. Flesh out the marked TODO sections below.

Nothing else in the codebase needs to change - the capture pipeline already
calls on_motion(), on_capture() etc. at the right moments.

We use gpiozero, which on the Pi 5 sits on top of lgpio (both already
installed).  gpiozero gives a friendly API (LED, Button, ...) and cleans up
pins automatically.

Typical future uses, already stubbed below:
    * A status LED that blinks each time a photo is taken.
    * A "motion live" LED that mirrors activity in the ROI.
    * An external hardware trigger (e.g. an infra-red beam-break across the
      finish line) that can force a capture independently of the camera-based
      motion detector - usually far more precise for timing.
"""

from __future__ import annotations

from typing import Callable

from . import config


class GpioController:
    def __init__(self):
        self._enabled = config.GPIO_ENABLED
        # Hardware objects are created in setup(); kept as attributes so we can
        # drive and later release them.
        self._capture_led = None
        self._motion_led = None
        self._external_trigger = None
        # Callback the capture service registers so an external trigger can ask
        # for a photo.  Stored here, invoked from the trigger's edge handler.
        self._trigger_callback: Callable[[], None] | None = None

    # -- lifecycle ----------------------------------------------------------

    def setup(self, trigger_callback: Callable[[], None] | None = None):
        """
        Initialise GPIO hardware.  Does nothing unless GPIO_ENABLED is True.

        `trigger_callback` is invoked whenever the external trigger fires; the
        capture service passes in its own "take a photo now" function here.
        """
        self._trigger_callback = trigger_callback
        if not self._enabled:
            print("[gpio] disabled (config.GPIO_ENABLED is False) - no-op mode")
            return

        # ---- Real GPIO setup (only runs when you enable it) ----------------
        try:
            from gpiozero import LED, Button

            self._capture_led = LED(config.GPIO_CAPTURE_LED_PIN)
            self._motion_led = LED(config.GPIO_MOTION_LED_PIN)

            # An external beam-break / push-button that forces a capture.
            self._external_trigger = Button(config.GPIO_EXTERNAL_TRIGGER_PIN)
            self._external_trigger.when_pressed = self._on_external_trigger

            print("[gpio] initialised")
        except Exception as exc:
            # Never let a GPIO problem take down the whole capture system.
            print(f"[gpio] setup failed ({exc}); continuing without GPIO")
            self._enabled = False

    def cleanup(self):
        """Release pins on shutdown."""
        for dev in (self._capture_led, self._motion_led, self._external_trigger):
            try:
                if dev is not None:
                    dev.close()
            except Exception:
                pass

    # -- event hooks called by the rest of the program ----------------------

    def on_motion(self, detected: bool):
        """Called every frame with the current motion state of the ROI."""
        if not self._enabled or self._motion_led is None:
            return
        # TODO: mirror motion on an LED.  Example:
        #   self._motion_led.on() if detected else self._motion_led.off()
        if detected:
            self._motion_led.on()
        else:
            self._motion_led.off()

    def on_capture(self, capture_id: int, bib_numbers: list[str]):
        """Called once per saved photo, after OCR has run."""
        if not self._enabled or self._capture_led is None:
            return
        # TODO: richer feedback, e.g. blink N times for N bibs, or beep.
        # `blink(... background=True)` returns immediately so we never stall
        # the capture pipeline.
        self._capture_led.blink(on_time=0.1, off_time=0.1, n=1, background=True)

    # -- internal -----------------------------------------------------------

    def _on_external_trigger(self):
        """Edge handler for the hardware trigger input."""
        print("[gpio] external trigger fired")
        if self._trigger_callback is not None:
            self._trigger_callback()

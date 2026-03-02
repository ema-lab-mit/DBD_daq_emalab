
import json
import os
from typing import Dict, Any

class SettingsManager:
    DEFAULT_SETTINGS = {
        "scan_settings": {
            "start_wn": 16666.0,
            "end_wn": 16680.0,
            "step_size": 0.5,
            "stop_mode": "bunches",
            "stop_val": 100,
            "loops": 1
        },
        "gui_settings": {
            "window_width": 1200,
            "window_height": 800,
            "refresh_rate_ms": 500
        },
        "data_settings": {
            "default_save_dir": "data",
            "auto_save": True
        },
        "simulation_settings": {
            "tagger": {
                "repetition_rate": 50.0,
                "mean_events_per_bunch": 200.0
            },
            "laser": {
                "move_speed": 10.0,
                "noise_level": 0.001
            },
            "multimeter": {
                "noise_level": 0.05
            }
        },
        "control_settings": {
            "laser": {
                "controller_mode": "pid",
                "tolerance": 0.007,
                "poll_interval": 0.01,
                "required_stable_samples": 4,
                "wavechannel": 1,
                "voltage_min": 0.0,
                "voltage_max": 5.0,
                "step_fine": 0.001,
                "step_coarse": 0.005,
                "pid": {
                    "kp": 0.02,
                    "ki": 0.005,
                    "kd": 0.0,
                    "d_filter_coeff": 0.1
                }
            }
        }
    }

    def __init__(self, config_path: str = "settings.json"):
        self.config_path = config_path
        self.settings = self.load_settings()

    def load_settings(self) -> Dict[str, Any]:
        """Loads settings from JSON file. Creates file with defaults if not exists."""
        if not os.path.exists(self.config_path):
            self.save_settings(self.DEFAULT_SETTINGS)
            return self.DEFAULT_SETTINGS.copy()

        try:
            with open(self.config_path, 'r') as f:
                user_settings = json.load(f)

            # Merge with defaults to ensure all keys exist
            # This is a shallow merge for sections
            merged = self.DEFAULT_SETTINGS.copy()
            for section, values in user_settings.items():
                if section in merged:
                    merged[section].update(values)
                else:
                    merged[section] = values
            return merged
        except Exception as e:
            print(f"Error loading settings: {e}. Using defaults.")
            return self.DEFAULT_SETTINGS.copy()

    def save_settings(self, settings: Dict[str, Any] = None):
        """Saves current settings to JSON file."""
        if settings is None:
            settings = self.settings

        try:
            with open(self.config_path, 'w') as f:
                json.dump(settings, f, indent=4)
        except Exception as e:
            print(f"Error saving settings: {e}")

    def get_section(self, section: str) -> Dict[str, Any]:
        return self.settings.get(section, {})

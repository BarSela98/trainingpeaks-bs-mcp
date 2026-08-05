#!/usr/bin/env python3
"""
Calculate TrainingPeaks workout intensities for any athlete.

Usage:
    python scripts/calculate_intensity.py --threshold 3.5842 --pace 4:30 --pace 5:20 --pace 6:00
    python scripts/calculate_intensity.py --ftp 280 --watts 230 --watts 250

Examples:
    # For a runner with threshold 3.5842 m/s (4:39/km)
    python scripts/calculate_intensity.py --threshold 3.5842 --pace 4:30 4:40 5:00 5:20 5:40 6:00

    # For a cyclist with FTP 280W
    python scripts/calculate_intensity.py --ftp 280 --watts 200 230 250 280
"""

import argparse
from typing import Union


def parse_pace(pace_str: str) -> int:
    """Convert pace string (M:SS) to seconds per km."""
    parts = pace_str.split(':')
    if len(parts) != 2:
        raise ValueError(f"Invalid pace format: {pace_str}. Use M:SS format (e.g., 4:30)")
    minutes = int(parts[0])
    seconds = int(parts[1])
    return minutes * 60 + seconds


def pace_to_m_s(pace_sec_km: int) -> float:
    """Convert pace (sec/km) to speed (m/s)."""
    return 1000 / pace_sec_km


def sec_to_pace(sec_km: int) -> str:
    """Convert seconds per km to M:SS format."""
    minutes = sec_km // 60
    seconds = sec_km % 60
    return f"{minutes}:{seconds:02d}"


def calculate_running_intensity(
    threshold_m_s: float, target_pace_str: str
) -> tuple[float, str, str]:
    """
    Calculate running intensity as % of threshold pace.

    Args:
        threshold_m_s: Athlete's speed threshold in m/s
        target_pace_str: Target pace in M:SS format

    Returns:
        Tuple of (intensity_percent, target_speed_m_s, zone_name)
    """
    pace_sec_km = parse_pace(target_pace_str)
    target_speed_m_s = pace_to_m_s(pace_sec_km)
    intensity_pct = (target_speed_m_s / threshold_m_s) * 100

    # Determine zone
    if intensity_pct <= 80:
        zone = "Z1 easy"
    elif intensity_pct <= 90:
        zone = "Z2 aerobic"
    elif intensity_pct <= 100:
        zone = "Z3 tempo"
    elif intensity_pct <= 106:
        zone = "Z4 threshold"
    elif intensity_pct <= 112:
        zone = "Z5a VO2"
    else:
        zone = "Z5b+ anaerobic"

    return (intensity_pct, target_speed_m_s, zone)


def calculate_cycling_intensity(ftp_watts: float, target_watts: float) -> tuple[float, str]:
    """
    Calculate cycling intensity as % of FTP.

    Args:
        ftp_watts: Athlete's FTP in watts
        target_watts: Target power in watts

    Returns:
        Tuple of (intensity_percent, zone_name)
    """
    intensity_pct = (target_watts / ftp_watts) * 100

    # Determine zone (typical TrainingPeaks zones)
    if intensity_pct < 56:
        zone = "Z1 active recovery"
    elif intensity_pct < 75:
        zone = "Z2 endurance"
    elif intensity_pct < 90:
        zone = "Z3 tempo"
    elif intensity_pct < 105:
        zone = "Z4 lactate threshold"
    elif intensity_pct < 120:
        zone = "Z5a VO2 max"
    elif intensity_pct < 150:
        zone = "Z5b anaerobic"
    else:
        zone = "Z5c neuromuscular power"

    return (intensity_pct, zone)


def main():
    parser = argparse.ArgumentParser(
        description="Calculate TrainingPeaks workout intensities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Running options
    parser.add_argument("--threshold", type=float, help="Running threshold speed (m/s)")
    parser.add_argument("--pace", nargs="+", help="Target paces (M:SS format)")

    # Cycling options
    parser.add_argument("--ftp", type=float, help="Cycling FTP (watts)")
    parser.add_argument("--watts", nargs="+", type=float, help="Target power values (watts)")

    args = parser.parse_args()

    if args.threshold and args.pace:
        print(f"\n🏃 Running Intensities (threshold: {args.threshold} m/s)")
        print(f"   = {sec_to_pace(int(1000 / args.threshold))}/km\n")
        print(f"{'Pace':<12} {'Speed (m/s)':<15} {'% Threshold':<15} {'Zone':<20}")
        print("-" * 62)

        for pace_str in args.pace:
            intensity_pct, speed_m_s, zone = calculate_running_intensity(args.threshold, pace_str)
            print(
                f"{pace_str:<12} {speed_m_s:<15.3f} {intensity_pct:<15.1f} {zone:<20}"
            )

    elif args.ftp and args.watts:
        print(f"\n🚴 Cycling Intensities (FTP: {args.ftp}W)\n")
        print(f"{'Power (W)':<12} {'% FTP':<15} {'Zone':<30}")
        print("-" * 57)

        for watts in args.watts:
            intensity_pct, zone = calculate_cycling_intensity(args.ftp, watts)
            print(f"{watts:<12.0f} {intensity_pct:<15.1f} {zone:<30}")

    else:
        parser.print_help()
        print("\n❌ Error: Provide either --threshold + --pace or --ftp + --watts")
        exit(1)


if __name__ == "__main__":
    main()

"""Project entry point for running the detector from the repository root."""

from core.detector import DoorNumberDetector


def main():
    detector = DoorNumberDetector()
    result = detector.detect_door_number(40.80082, -8.593741)

    print("\n" + "=" * 50)
    print("DETECTION RESULT")
    print("=" * 50)
    print(f"Latitude: {result['latitude']}")
    print(f"Longitude: {result['longitude']}")
    print(f"Door number: {result.get('door_number', 'N/A')}")
    print(f"Confidence: {result.get('confidence', 0)}%")
    print(f"Status: {'✓ Success' if result['success'] else '✗ Error'}")
    if not result['success']:
        print(f"Error: {result.get('error', 'Unknown')}")
    print("=" * 50)

    detector.close()


if __name__ == "__main__":
    main()

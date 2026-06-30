"""Project entry point for running the detector from the repository root."""

from core.detector import DoorNumberDetector


def main():
    detector = DoorNumberDetector()
    latitude, longitude = 41.256331, -8.645468  # 36 simple
    #latitude, longitude = 38.024463, -7.712959  # 17 
    #latitude, longitude = 39.701068, -8.910996 #14 very difficult

    # Mostra a imagem padrão apenas para preview — não interfere com a deteção
    preview_image = detector.street_view.get_image(latitude, longitude)
    if preview_image is not None:
        print("Displaying fetched Street View image...")
        preview_image.show()

    # Corre o loop completo: FOV [90, 60, 40] × pitch × heading offsets
    result = detector.detect_door_number(latitude, longitude)

    print("\n" + "=" * 50)
    print("DETECTION RESULT")
    print("=" * 50)
    print(f"Latitude: {result['latitude']}")
    print(f"Longitude: {result['longitude']}")
    print(f"Door number: {result.get('door_number', 'N/A')}")
    print(f"Confidence: {result.get('confidence', 0)}%")
    print(f"Status: {'✓ Success' if result['success'] else '✗ Error'}")
    if not result["success"]:
        print(f"Error: {result.get('error', 'Unknown')}")
    print("=" * 50)

    detector.close()


if __name__ == "__main__":
    main()

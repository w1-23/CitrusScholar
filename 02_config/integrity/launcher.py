"""Host-bound script bytes for the sealed offline Code Worker image."""
import sys

# The Worker boots isolated; neither authorized data nor the request is an
# import directory. Both import roots below are immutable contents of the SIF.
sys.path[:0] = ["/opt/citrus-integrity/addons", "/opt/citrus-integrity"]
from numeric_integrity.worker import main

if __name__ == "__main__":
    main()

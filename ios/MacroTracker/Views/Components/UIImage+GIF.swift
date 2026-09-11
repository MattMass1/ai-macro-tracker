import UIKit
import ImageIO

extension UIImage {
    /// Build an animated UIImage from GIF data (iOS 13+). Returns nil if the
    /// data isn't a decodable GIF.
    static func gif(data: Data) -> UIImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let count = CGImageSourceGetCount(source)
        guard count > 0 else { return nil }

        var images: [UIImage] = []
        var duration: Double = 0

        for i in 0..<count {
            guard let cgImage = CGImageSourceCreateImageAtIndex(source, i, nil) else { continue }
            images.append(UIImage(cgImage: cgImage))
            // Per-frame delay from the GIF properties (fallback 0.1s).
            if let props = CGImageSourceCopyPropertiesAtIndex(source, i, nil) as? [CFString: Any],
               let gifProps = props[kCGImagePropertyGIFDictionary] as? [CFString: Any],
               let delay = gifProps[kCGImagePropertyGIFUnclampedDelayTime] as? Double ?? gifProps[kCGImagePropertyGIFDelayTime] as? Double {
                duration += max(delay, 0.02)
            } else {
                duration += 0.1
            }
        }

        guard !images.isEmpty else { return nil }
        if images.count == 1 {
            // Static GIF — return the single frame.
            return images[0]
        }
        return UIImage.animatedImage(with: images, duration: duration)
    }
}

// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "PlaidifyLinkKit",
    platforms: [
        .iOS(.v15),
        .macOS(.v13),
    ],
    products: [
        .library(
            name: "PlaidifyLinkKit",
            targets: ["PlaidifyLinkKit"]
        ),
    ],
    targets: [
        .target(
            name: "PlaidifyLinkKit"
        ),
        .testTarget(
            name: "PlaidifyLinkKitTests",
            dependencies: ["PlaidifyLinkKit"]
        ),
    ]
)
// gflbench: a ray-marched Metal benchmark renderer with per-pass GPU timing.
//
// Build:  bash bench/metal/build.sh        (needs the Xcode command line tools)
// Run:    bench/metal/gflbench --preset high --resolution 2560x1440 --duration 60 --output run.csv
//
// The scene is drawn by four compute passes. Each pass is its own command
// buffer, so the GPU start and end timestamps of every pass can be read
// separately, which is what makes a "where does the GPU time go" breakdown
// possible without an external profiler:
//
//   gbuffer    primary ray march: hit position, depth, surface normal
//   shadows    direct light from N point lights with ray-marched soft shadows
//   ao         ambient occlusion from distance-field samples around the normal
//   composite  lighting + ambient * AO + fog, tonemapped into the window
//
// Output: one CSV row per frame. Shared metrics use PresentMon's column names
// (CPUStartTime, MsBetweenPresents, MsGPUBusy, ...) so the gfl analysis code
// reads it like a PresentMon log. Extra columns: MsPass_<name> (GPU ms per pass)
// and MetalAllocatedMiB. A JSON file next to the CSV records the device, OS,
// preset parameters, and options.

import AppKit
import CoreGraphics
import Foundation
import ImageIO
import Metal
import QuartzCore

// MARK: - Options and presets

let usage = """
usage: gflbench [--preset low|medium|high|epic] [--resolution WIDTHxHEIGHT]
                [--duration SECONDS] [--output FILE.csv]
                [--shadows-scale 0..1] [--ao-scale 0..1] [--vsync]
                [--upsample weighted|edge]
                [--still FILE.png [--scene-time SECONDS]]

  --shadows-scale, --ao-scale  render that pass at a fraction of the output
                               resolution (0.5 = half width and height), then
                               upsample with a depth-aware filter
  --vsync                      sync to the display (off by default, so frame
                               times show the real cost of each frame)
  --upsample weighted|edge     how reduced-resolution passes are filled back in:
                               weighted (default, the original filter) or edge
                               (blends only samples on the same surface and
                               computes edge pixels at full resolution)
  --still FILE.png             render one frame and save it as a PNG instead of
                               benchmarking (no timing log is written)
  --scene-time SECONDS         where on the camera path the still is taken
                               (default 5); the same time gives the same view
"""

struct Options {
    var preset = "high"
    var width = 1920
    var height = 1080
    var duration = 60.0
    var output = "gflbench.csv"
    var shadowsScale = 1.0
    var aoScale = 1.0
    var vsync = false
    var upsample = "weighted"
    var stillPath: String? = nil
    var sceneTime = 5.0
}

// What each preset changes. Every knob scales real GPU work:
//   marchSteps     max steps of the primary ray march
//   shadowSteps    max steps per shadow ray (one shadow ray per light per pixel)
//   lightCount     point lights, each with its own soft shadow
//   aoDirections   AO sample directions per pixel
//   aoSteps        distance-field samples along each AO direction
//   detailOctaves  surface detail layers added to every distance evaluation
struct Quality {
    let marchSteps: Int32
    let shadowSteps: Int32
    let lightCount: Int32
    let aoDirections: Int32
    let aoSteps: Int32
    let detailOctaves: Int32

    static let presets: [String: Quality] = [
        "low":    Quality(marchSteps: 64,  shadowSteps: 16, lightCount: 1, aoDirections: 2, aoSteps: 3, detailOctaves: 0),
        "medium": Quality(marchSteps: 96,  shadowSteps: 24, lightCount: 2, aoDirections: 4, aoSteps: 4, detailOctaves: 1),
        "high":   Quality(marchSteps: 128, shadowSteps: 32, lightCount: 3, aoDirections: 6, aoSteps: 5, detailOctaves: 2),
        "epic":   Quality(marchSteps: 192, shadowSteps: 48, lightCount: 4, aoDirections: 8, aoSteps: 6, detailOctaves: 3),
    ]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("gflbench: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

func parseOptions() -> Options {
    var options = Options()
    var args = Array(CommandLine.arguments.dropFirst())

    func value(_ flag: String) -> String {
        guard !args.isEmpty else { fail("missing value for \(flag)") }
        return args.removeFirst()
    }
    func fraction(_ flag: String) -> Double {
        guard let v = Double(value(flag)), v > 0, v <= 1 else { fail("\(flag) must be in (0, 1]") }
        return v
    }

    while !args.isEmpty {
        let flag = args.removeFirst()
        switch flag {
        case "--preset":
            options.preset = value(flag)
        case "--resolution":
            let parts = value(flag).lowercased().split(separator: "x")
            guard parts.count == 2, let w = Int(parts[0]), let h = Int(parts[1]), w > 0, h > 0 else {
                fail("--resolution must look like 2560x1440")
            }
            options.width = w
            options.height = h
        case "--duration":
            guard let d = Double(value(flag)), d > 0 else { fail("--duration must be a positive number") }
            options.duration = d
        case "--output":
            options.output = value(flag)
        case "--shadows-scale":
            options.shadowsScale = fraction(flag)
        case "--ao-scale":
            options.aoScale = fraction(flag)
        case "--vsync":
            options.vsync = true
        case "--upsample":
            let mode = value(flag)
            guard mode == "weighted" || mode == "edge" else { fail("--upsample must be weighted or edge") }
            options.upsample = mode
        case "--still":
            options.stillPath = value(flag)
        case "--scene-time":
            guard let t = Double(value(flag)), t >= 0 else { fail("--scene-time must be a number >= 0") }
            options.sceneTime = t
        case "--help", "-h":
            print(usage)
            exit(0)
        default:
            fail("unknown option \(flag)\n\(usage)")
        }
    }
    guard Quality.presets[options.preset] != nil else {
        fail("unknown preset \(options.preset); use low, medium, high, or epic")
    }
    return options
}

// Must match `struct Params` in the shader source byte for byte: twelve
// 4-byte fields, 48 bytes, no padding.
// upsampleMode: 0 = depth-weighted bilinear (original), 1 = edge-aware.
struct Params {
    var width: UInt32
    var height: UInt32
    var outWidth: UInt32   // size of the texture this pass writes
    var outHeight: UInt32
    var time: Float
    var marchSteps: Int32
    var shadowSteps: Int32
    var lightCount: Int32
    var aoDirections: Int32
    var aoSteps: Int32
    var detailOctaves: Int32
    var upsampleMode: Int32
}

func ms(_ seconds: Double) -> String { String(format: "%.4f", seconds * 1000.0) }

// MARK: - Per-frame records

// Filled from several threads: the render thread creates a row, and Metal's
// completion and presentation handlers fill in GPU and display timestamps.
final class FrameLog: @unchecked Sendable {
    struct Row {
        var cpuStart = 0.0      // seconds since the run started
        var presentCall = 0.0   // host time when the frame was handed to the display
        var cpuBusy = 0.0       // seconds spent encoding the frame on the CPU
        var passStart = [Double](repeating: 0, count: 4)  // host time, from Metal
        var passEnd = [Double](repeating: 0, count: 4)
        var presented = 0.0     // host time the frame reached the screen (0 = never shown)
        var allocatedMiB = 0.0
    }

    private var rows: [Row] = []
    private let lock = NSLock()

    func append(_ row: Row) -> Int {
        lock.lock(); defer { lock.unlock() }
        rows.append(row)
        return rows.count - 1
    }

    func update(_ index: Int, _ body: (inout Row) -> Void) {
        lock.lock(); defer { lock.unlock() }
        body(&rows[index])
    }

    func snapshot() -> [Row] {
        lock.lock(); defer { lock.unlock() }
        return rows
    }
}

// MARK: - Renderer

final class Bench: @unchecked Sendable {
    static let passNames = ["gbuffer", "shadows", "ao", "composite"]
    static let maxFramesInFlight = 3

    let options: Options
    let quality: Quality
    let device: MTLDevice
    let queue: MTLCommandQueue
    let layer: CAMetalLayer
    let pipelines: [MTLComputePipelineState]
    let positionTexture: MTLTexture  // xyz = hit position, w = distance (-1 = sky)
    let normalTexture: MTLTexture
    let lightTexture: MTLTexture     // direct light, possibly at reduced resolution
    let aoTexture: MTLTexture        // ambient occlusion, possibly at reduced resolution
    let frames = FrameLog()
    let inFlight = DispatchSemaphore(value: Bench.maxFramesInFlight)

    init(options: Options, device: MTLDevice, layer: CAMetalLayer) {
        self.options = options
        self.quality = Quality.presets[options.preset]!
        self.device = device
        self.layer = layer
        guard let queue = device.makeCommandQueue() else { fail("could not create a command queue") }
        self.queue = queue

        let library: MTLLibrary
        do {
            library = try device.makeLibrary(source: shaderSource, options: nil)
        } catch {
            fail("shader compilation failed:\n\(error)")
        }
        let kernelNames = ["gbuffer_kernel", "shadows_kernel", "ao_kernel", "composite_kernel"]
        self.pipelines = kernelNames.map { (name: String) -> MTLComputePipelineState in
            guard let function = library.makeFunction(name: name) else { fail("missing kernel \(name)") }
            do {
                return try device.makeComputePipelineState(function: function)
            } catch {
                fail("could not build pipeline \(name): \(error)")
            }
        }

        let w = options.width, h = options.height
        positionTexture = Bench.makeTexture(device, .rgba32Float, w, h)
        normalTexture = Bench.makeTexture(device, .rgba16Float, w, h)
        lightTexture = Bench.makeTexture(device, .rgba16Float, Bench.scaled(w, options.shadowsScale),
                                         Bench.scaled(h, options.shadowsScale))
        aoTexture = Bench.makeTexture(device, .r16Float, Bench.scaled(w, options.aoScale),
                                      Bench.scaled(h, options.aoScale))
    }

    static func scaled(_ size: Int, _ scale: Double) -> Int {
        max(1, Int((Double(size) * scale).rounded()))
    }

    static func makeTexture(_ device: MTLDevice, _ format: MTLPixelFormat, _ w: Int, _ h: Int) -> MTLTexture {
        let descriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: format, width: w, height: h,
                                                                  mipmapped: false)
        descriptor.usage = [.shaderRead, .shaderWrite]
        descriptor.storageMode = .private
        guard let texture = device.makeTexture(descriptor: descriptor) else {
            fail("could not allocate a \(w)x\(h) texture")
        }
        return texture
    }

    func dispatch(_ encoder: MTLComputeCommandEncoder, _ pipeline: MTLComputePipelineState, _ w: Int, _ h: Int) {
        let tw = pipeline.threadExecutionWidth
        let th = max(1, min(8, pipeline.maxTotalThreadsPerThreadgroup / tw))
        encoder.setComputePipelineState(pipeline)
        encoder.dispatchThreads(MTLSize(width: w, height: h, depth: 1),
                                threadsPerThreadgroup: MTLSize(width: tw, height: th, depth: 1))
    }

    // Render until the duration is up, write the log, and exit.
    func run() {
        let expectedSize = CGSize(width: options.width, height: options.height)
        let start = CACurrentMediaTime()
        while CACurrentMediaTime() - start < options.duration {
            inFlight.wait()
            if layer.drawableSize != expectedSize { layer.drawableSize = expectedSize }
            autoreleasepool { renderFrame(start: start) }
        }
        // Wait for all submitted GPU work, then give the last presentation
        // callbacks a moment to arrive before writing the log.
        if let last = queue.makeCommandBuffer() {
            last.commit()
            last.waitUntilCompleted()
        }
        Thread.sleep(forTimeInterval: 0.5)
        writeOutputs()
        exit(0)
    }

    /// Encodes one render pass. Shared by the timed loop and by still images,
    /// so a still shows exactly what the benchmark renders.
    func encode(pass: Int, encoder: MTLComputeCommandEncoder, time: Float, target: MTLTexture) {
        let outputSizes = [
            (options.width, options.height),
            (lightTexture.width, lightTexture.height),
            (aoTexture.width, aoTexture.height),
            (options.width, options.height),
        ]
        var params = Params(width: UInt32(options.width), height: UInt32(options.height),
                            outWidth: UInt32(outputSizes[pass].0), outHeight: UInt32(outputSizes[pass].1),
                            time: time,
                            marchSteps: quality.marchSteps, shadowSteps: quality.shadowSteps,
                            lightCount: quality.lightCount, aoDirections: quality.aoDirections,
                            aoSteps: quality.aoSteps, detailOctaves: quality.detailOctaves,
                            upsampleMode: options.upsample == "edge" ? 1 : 0)
        encoder.setBytes(&params, length: MemoryLayout<Params>.stride, index: 0)
        switch pass {
        case 0:
            encoder.setTexture(positionTexture, index: 0)
            encoder.setTexture(normalTexture, index: 1)
        case 1:
            encoder.setTexture(positionTexture, index: 0)
            encoder.setTexture(normalTexture, index: 1)
            encoder.setTexture(lightTexture, index: 2)
        case 2:
            encoder.setTexture(positionTexture, index: 0)
            encoder.setTexture(normalTexture, index: 1)
            encoder.setTexture(aoTexture, index: 2)
        default:
            encoder.setTexture(positionTexture, index: 0)
            encoder.setTexture(normalTexture, index: 1)
            encoder.setTexture(lightTexture, index: 2)
            encoder.setTexture(aoTexture, index: 3)
            encoder.setTexture(target, index: 4)
        }
        dispatch(encoder, pipelines[pass], outputSizes[pass].0, outputSizes[pass].1)
    }

    /// Renders one frame at a fixed point on the camera path and saves it as a
    /// PNG. The same scene time always gives the same camera and light
    /// positions, so stills from different presets or options line up exactly.
    func saveStill(to path: String, sceneTime: Double) -> Never {
        let w = options.width, h = options.height
        let descriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: w, height: h,
                                                                  mipmapped: false)
        descriptor.usage = [.shaderRead, .shaderWrite]
        descriptor.storageMode = .shared
        guard let target = device.makeTexture(descriptor: descriptor),
              let commandBuffer = queue.makeCommandBuffer() else {
            fail("could not allocate the still image")
        }
        for pass in 0..<Bench.passNames.count {
            guard let encoder = commandBuffer.makeComputeCommandEncoder() else { fail("could not create an encoder") }
            encode(pass: pass, encoder: encoder, time: Float(sceneTime), target: target)
            encoder.endEncoding()
        }
        commandBuffer.commit()
        commandBuffer.waitUntilCompleted()

        let bytesPerRow = w * 4
        var pixels = [UInt8](repeating: 0, count: bytesPerRow * h)
        target.getBytes(&pixels, bytesPerRow: bytesPerRow, from: MTLRegionMake2D(0, 0, w, h), mipmapLevel: 0)
        // The texture holds B, G, R, A bytes. Read as little-endian 32-bit
        // words that is alpha-first ARGB, which is what this bitmap info says.
        let bitmapInfo = CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue
                                      | CGBitmapInfo.byteOrder32Little.rawValue)
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let provider = CGDataProvider(data: Data(pixels) as CFData),
              let image = CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32,
                                  bytesPerRow: bytesPerRow, space: colorSpace, bitmapInfo: bitmapInfo,
                                  provider: provider, decode: nil, shouldInterpolate: false,
                                  intent: .defaultIntent),
              let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL,
                                                                "public.png" as CFString, 1, nil) else {
            fail("could not encode \(path) as a PNG")
        }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { fail("could not write \(path)") }
        print("gflbench: wrote still image \(path) (scene time \(sceneTime) s)")
        exit(0)
    }

    func renderFrame(start: CFTimeInterval) {
        let frameBegin = CACurrentMediaTime()
        guard let drawable = layer.nextDrawable() else {
            inFlight.signal()
            return
        }
        let encodeBegin = CACurrentMediaTime()

        var row = FrameLog.Row()
        row.cpuStart = frameBegin - start
        row.allocatedMiB = Double(device.currentAllocatedSize) / 1_048_576.0
        let index = frames.append(row)
        let frames = self.frames
        let inFlight = self.inFlight

        let time = Float(frameBegin - start)
        for pass in 0..<Bench.passNames.count {
            guard let commandBuffer = queue.makeCommandBuffer(),
                  let encoder = commandBuffer.makeComputeCommandEncoder() else {
                fail("could not create a command buffer")
            }
            commandBuffer.label = Bench.passNames[pass]
            encode(pass: pass, encoder: encoder, time: time, target: drawable.texture)
            encoder.endEncoding()

            commandBuffer.addCompletedHandler { buffer in
                frames.update(index) { r in
                    r.passStart[pass] = buffer.gpuStartTime
                    r.passEnd[pass] = buffer.gpuEndTime
                }
            }
            if pass == Bench.passNames.count - 1 {
                drawable.addPresentedHandler { shown in
                    frames.update(index) { r in r.presented = shown.presentedTime }
                }
                commandBuffer.present(drawable)
                commandBuffer.addCompletedHandler { _ in inFlight.signal() }
                let now = CACurrentMediaTime()
                frames.update(index) { r in
                    r.presentCall = now
                    r.cpuBusy = now - encodeBegin
                }
            }
            commandBuffer.commit()
        }
    }

    func writeOutputs() {
        let rows = frames.snapshot()
        let passHeader = Bench.passNames.map { "MsPass_\($0)" }.joined(separator: ",")
        var lines = ["Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,"
                     + "AllowsTearing,PresentMode,CPUStartTime,MsCPUBusy,MsGPUTime,MsGPUBusy,"
                     + "MsBetweenPresents,MsBetweenDisplayChange,\(passHeader),MetalAllocatedMiB"]
        let pid = ProcessInfo.processInfo.processIdentifier
        let sync = options.vsync ? 1 : 0
        let missing = Array(repeating: "NA", count: Bench.passNames.count).joined(separator: ",")
        var previousPresent: Double? = nil
        var previousDisplay: Double? = nil

        for r in rows where r.presentCall > 0 {
            let timed = r.passStart.allSatisfy { $0 > 0 } && r.passEnd.allSatisfy { $0 > 0 }
            let passSeconds = (0..<Bench.passNames.count).map { r.passEnd[$0] - r.passStart[$0] }
            let gpuBusy = timed ? ms(passSeconds.reduce(0, +)) : "NA"
            let gpuTime = timed ? ms(r.passEnd.max()! - r.passStart.min()!) : "NA"
            let passCells = timed ? passSeconds.map { ms($0) }.joined(separator: ",") : missing
            let between = previousPresent.map { ms(r.presentCall - $0) } ?? "NA"
            var display = "NA"
            if r.presented > 0 {
                if let previous = previousDisplay { display = ms(r.presented - previous) }
                previousDisplay = r.presented
            }
            previousPresent = r.presentCall
            let cpuStart = String(format: "%.6f", r.cpuStart)
            let memory = String(format: "%.1f", r.allocatedMiB)
            lines.append("gflbench,\(pid),CAMetalLayer,Metal,\(sync),0,0,CAMetalLayer,\(cpuStart),"
                         + "\(ms(r.cpuBusy)),\(gpuTime),\(gpuBusy),\(between),\(display),\(passCells),\(memory)")
        }

        let csv = lines.joined(separator: "\n") + "\n"
        do {
            try csv.write(toFile: options.output, atomically: true, encoding: .utf8)
        } catch {
            fail("could not write \(options.output): \(error)")
        }

        let metadata: [String: Any] = [
            "tool": "gflbench",
            "version": "1.0",
            "device": device.name,
            "os": ProcessInfo.processInfo.operatingSystemVersionString,
            "preset": options.preset,
            "width": options.width,
            "height": options.height,
            "duration_s": options.duration,
            "shadows_scale": options.shadowsScale,
            "ao_scale": options.aoScale,
            "upsample": options.upsample,
            "vsync": options.vsync,
            "frames": lines.count - 1,
            "quality": [
                "march_steps": Int(quality.marchSteps),
                "shadow_steps": Int(quality.shadowSteps),
                "light_count": Int(quality.lightCount),
                "ao_directions": Int(quality.aoDirections),
                "ao_steps": Int(quality.aoSteps),
                "detail_octaves": Int(quality.detailOctaves),
            ],
        ]
        let jsonURL = URL(fileURLWithPath: options.output).deletingPathExtension().appendingPathExtension("json")
        if let data = try? JSONSerialization.data(withJSONObject: metadata, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: jsonURL)
        }
        print("gflbench: wrote \(lines.count - 1) frames to \(options.output)")
    }
}

// MARK: - Window

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let options: Options
    var window: NSWindow?
    var bench: Bench?

    init(options: Options) {
        self.options = options
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let device = MTLCreateSystemDefaultDevice() else { fail("no Metal device found") }

        let layer = CAMetalLayer()
        layer.device = device
        layer.pixelFormat = .bgra8Unorm
        layer.framebufferOnly = false  // the composite pass writes to the drawable from a compute kernel
        layer.maximumDrawableCount = Bench.maxFramesInFlight
        layer.displaySyncEnabled = options.vsync
        layer.drawableSize = CGSize(width: options.width, height: options.height)

        // The window is sized to fit the screen; the drawable stays at the
        // requested resolution and is scaled for display, so a 4K run is
        // still rendered at 3840x2160 on a smaller screen.
        let screen = NSScreen.main
        let scale = screen?.backingScaleFactor ?? 2.0
        let visible = screen?.visibleFrame.size ?? CGSize(width: 1440, height: 900)
        var size = CGSize(width: CGFloat(options.width) / scale, height: CGFloat(options.height) / scale)
        let fit = min(1.0, visible.width * 0.9 / size.width, visible.height * 0.9 / size.height)
        size = CGSize(width: size.width * fit, height: size.height * fit)

        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.titled, .closable, .miniaturizable],
                              backing: .buffered, defer: false)
        window.title = "gflbench: \(options.preset) \(options.width)x\(options.height)"
        let view = NSView(frame: NSRect(origin: .zero, size: size))
        view.layer = layer
        view.wantsLayer = true
        window.contentView = view
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.window = window

        let bench = Bench(options: options, device: device, layer: layer)
        self.bench = bench
        let opts = self.options  // a copy the background thread can capture
        let thread: Thread
        if let still = opts.stillPath {
            print("gflbench: \(device.name), preset \(opts.preset), "
                  + "\(opts.width)x\(opts.height), still at scene time \(opts.sceneTime) s")
            thread = Thread { bench.saveStill(to: still, sceneTime: opts.sceneTime) }
        } else {
            print("gflbench: \(device.name), preset \(opts.preset), "
                  + "\(opts.width)x\(opts.height), \(opts.duration) s -> \(opts.output)")
            thread = Thread { bench.run() }
        }
        thread.qualityOfService = .userInteractive
        thread.start()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true  // closing the window aborts the run (no log is written)
    }
}

// MARK: - Shaders (compiled at startup, so the full Xcode app is not needed)

let shaderSource = """
#include <metal_stdlib>
using namespace metal;

struct Params {
    uint width;
    uint height;
    uint outWidth;
    uint outHeight;
    float time;
    int marchSteps;
    int shadowSteps;
    int lightCount;
    int aoDirections;
    int aoSteps;
    int detailOctaves;
    int upsampleMode;  // 0 = depth-weighted bilinear (original), 1 = edge-aware
};

constant float MAX_DIST = 80.0;

// ---- Scene: signed distance functions ----

float sdBox(float3 p, float3 b) {
    float3 q = abs(p) - b;
    return length(max(q, float3(0.0))) + min(max(q.x, max(q.y, q.z)), 0.0);
}

float sdTorus(float3 p, float2 t) {
    float2 q = float2(length(p.xz) - t.x, p.y);
    return length(q) - t.y;
}

float smin(float a, float b, float k) {
    float h = clamp(0.5 + 0.5 * (b - a) / k, 0.0, 1.0);
    return mix(b, a, h) - k * h * (1.0 - h);
}

// Layers of surface detail. Each layer adds work to every distance evaluation.
float detail(float3 p, int octaves) {
    float d = 0.0;
    float a = 0.03;
    float f = 3.0;
    for (int i = 0; i < octaves; i++) {
        d += a * sin(f * p.x) * sin(f * p.y + 1.3) * sin(f * p.z + 2.1);
        a *= 0.5;
        f *= 2.03;
    }
    return d;
}

// A ground plane and an endless grid of pillars topped with domes, each with a ring.
float map(float3 p, int octaves) {
    float2 cell = round(p.xz / 4.0);
    float3 q = p;
    q.x -= cell.x * 4.0;
    q.z -= cell.y * 4.0;
    float h = 0.8 + 0.4 * sin(cell.x * 1.7 + cell.y * 2.3);
    float body = smin(sdBox(q - float3(0.0, h, 0.0), float3(0.55, h, 0.55)),
                      length(q - float3(0.0, 2.0 * h + 0.45, 0.0)) - 0.7, 0.3);
    float ring = sdTorus(q - float3(0.0, 0.25, 0.0), float2(1.3, 0.12));
    float d = min(min(body, ring), p.y);
    return d + detail(p, octaves);
}

float3 calcNormal(float3 p, int octaves) {
    const float e = 0.0015;
    const float2 k = float2(1.0, -1.0);
    return normalize(k.xyy * map(p + k.xyy * e, octaves) +
                     k.yyx * map(p + k.yyx * e, octaves) +
                     k.yxy * map(p + k.yxy * e, octaves) +
                     k.xxx * map(p + k.xxx * e, octaves));
}

// Camera orbits once every 20 s, above all geometry, so every run sees the same path.
void cameraRay(float time, uint2 pixel, uint width, uint height, thread float3 &ro, thread float3 &rd) {
    float2 uv = (float2(pixel) + 0.5) / float2(float(width), float(height)) * 2.0 - 1.0;
    uv.y = -uv.y;
    uv.x *= float(width) / float(height);
    float a = time * 0.314159;
    ro = float3(9.0 * cos(a), 4.6 + 0.6 * sin(2.0 * a), 9.0 * sin(a));
    float3 target = float3(0.0, 0.5, 0.0);
    float3 ww = normalize(target - ro);
    float3 uu = normalize(cross(ww, float3(0.0, 1.0, 0.0)));
    float3 vv = cross(uu, ww);
    rd = normalize(uv.x * uu + uv.y * vv + 1.8 * ww);
}

// Lights circle above the scene.
float3 lightPosition(int i, float time) {
    float a = float(i) * 2.399 + time * 0.2;
    float r = 5.0 + 2.0 * float(i % 3);
    return float3(r * cos(a), 5.0 + 0.7 * float(i % 2), r * sin(a));
}

float3 lightColor(int i) {
    switch (i % 4) {
        case 0: return float3(1.00, 0.85, 0.70);
        case 1: return float3(0.60, 0.75, 1.00);
        case 2: return float3(1.00, 0.65, 0.60);
        default: return float3(0.70, 1.00, 0.75);
    }
}

// Which full-resolution pixel a (possibly reduced-resolution) output pixel reads.
uint2 sourcePixel(uint2 gid, constant Params &P) {
    float2 s = (float2(gid) + 0.5) * float2(float(P.width) / float(P.outWidth),
                                            float(P.height) / float(P.outHeight));
    return min(uint2(s), uint2(P.width - 1, P.height - 1));
}

// ---- Pass 1: primary ray march ----

kernel void gbuffer_kernel(texture2d<float, access::write> positionOut [[texture(0)]],
                           texture2d<float, access::write> normalOut [[texture(1)]],
                           constant Params &P [[buffer(0)]],
                           uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= P.width || gid.y >= P.height) return;
    float3 ro;
    float3 rd;
    cameraRay(P.time, gid, P.width, P.height, ro, rd);
    float t = 0.05;
    bool hit = false;
    for (int i = 0; i < P.marchSteps; i++) {
        float d = map(ro + rd * t, P.detailOctaves);
        if (d < 0.0008 * t) { hit = true; break; }
        t += d * 0.85;  // under-step: the detail layers make the distance field inexact
        if (t > MAX_DIST) break;
    }
    if (hit) {
        float3 p = ro + rd * t;
        positionOut.write(float4(p, t), gid);
        normalOut.write(float4(calcNormal(p, P.detailOctaves), 1.0), gid);
    } else {
        positionOut.write(float4(0.0, 0.0, 0.0, -1.0), gid);
        normalOut.write(float4(0.0), gid);
    }
}

// ---- Pass 2: direct light with soft shadows ----

float softShadow(float3 ro, float3 rd, float maxT, int steps, int octaves) {
    float res = 1.0;
    float t = 0.02;
    for (int i = 0; i < steps; i++) {
        float h = map(ro + rd * t, octaves);
        res = min(res, 10.0 * h / t);
        t += clamp(h, 0.03, 0.6);
        if (res < 0.002 || t > maxT) break;
    }
    return clamp(res, 0.0, 1.0);
}

// Direct light at one surface point: every light, each with a soft shadow.
// Used by the shadows pass and, for edge pixels, by the edge-aware composite.
float3 directLight(float3 surface, float3 n, constant Params &P) {
    float3 p = surface + n * 0.01;
    float3 sum = float3(0.0);
    for (int i = 0; i < P.lightCount; i++) {
        float3 toLight = lightPosition(i, P.time) - p;
        float dist = length(toLight);
        float3 l = toLight / dist;
        float ndl = dot(n, l);
        if (ndl <= 0.0) continue;
        float shadow = softShadow(p, l, dist, P.shadowSteps, P.detailOctaves);
        sum += lightColor(i) * ndl * shadow * (60.0 / (1.0 + dist * dist));
    }
    // Normalize by light count so every preset has a similar overall exposure.
    return sum / float(max(P.lightCount, 1));
}

kernel void shadows_kernel(texture2d<float, access::read> positionIn [[texture(0)]],
                           texture2d<float, access::read> normalIn [[texture(1)]],
                           texture2d<float, access::write> lightOut [[texture(2)]],
                           constant Params &P [[buffer(0)]],
                           uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= P.outWidth || gid.y >= P.outHeight) return;
    uint2 src = sourcePixel(gid, P);
    float4 pos = positionIn.read(src);
    if (pos.w < 0.0) { lightOut.write(float4(0.0), gid); return; }
    float3 n = normalIn.read(src).xyz;
    lightOut.write(float4(directLight(pos.xyz, n, P), 1.0), gid);
}

// ---- Pass 3: ambient occlusion ----

float3 hemisphereDirection(float3 n, int i, int count) {
    float z = 1.0 - (float(i) + 0.5) / float(count);
    float r = sqrt(max(0.0, 1.0 - z * z));
    float phi = 2.39996323 * float(i);
    float3 t = abs(n.y) < 0.99 ? cross(n, float3(0.0, 1.0, 0.0)) : cross(n, float3(1.0, 0.0, 0.0));
    t = normalize(t);
    float3 b = cross(n, t);
    return normalize(r * cos(phi) * t + r * sin(phi) * b + z * n);
}

// Ambient occlusion at one surface point. Used by the ao pass and, for edge
// pixels, by the edge-aware composite.
float ambientOcclusion(float3 surface, float3 n, constant Params &P) {
    float occlusion = 0.0;
    for (int d = 0; d < P.aoDirections; d++) {
        float3 dir = hemisphereDirection(n, d, P.aoDirections);
        float weight = 1.0;
        for (int s = 1; s <= P.aoSteps; s++) {
            float h = 0.04 + 0.6 * float(s) / float(P.aoSteps);
            occlusion += weight * max(0.0, h - map(surface + dir * h, P.detailOctaves));
            weight *= 0.7;
        }
    }
    return clamp(1.0 - 1.5 * occlusion / float(max(P.aoDirections, 1)), 0.0, 1.0);
}

kernel void ao_kernel(texture2d<float, access::read> positionIn [[texture(0)]],
                      texture2d<float, access::read> normalIn [[texture(1)]],
                      texture2d<float, access::write> aoOut [[texture(2)]],
                      constant Params &P [[buffer(0)]],
                      uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= P.outWidth || gid.y >= P.outHeight) return;
    uint2 src = sourcePixel(gid, P);
    float4 pos = positionIn.read(src);
    if (pos.w < 0.0) { aoOut.write(float4(1.0), gid); return; }
    float3 n = normalIn.read(src).xyz;
    aoOut.write(float4(ambientOcclusion(pos.xyz, n, P)), gid);
}

// ---- Pass 4: composite ----

float3 skyColor(float3 rd) {
    float k = clamp(rd.y * 0.5 + 0.5, 0.0, 1.0);
    return mix(float3(0.85, 0.80, 0.75), float3(0.35, 0.55, 0.85), float3(k));
}

// Depth-aware upsampling of a reduced-resolution pass: bilinear weights, scaled
// down for neighbors at a different depth so light does not bleed across edges.
float4 upsample(texture2d<float, access::read> lo, texture2d<float, access::read> positionIn,
                uint2 gid, float depth, constant Params &P) {
    uint lw = lo.get_width();
    uint lh = lo.get_height();
    if (lw == P.width && lh == P.height) return lo.read(gid);
    float2 f = (float2(gid) + 0.5) * float2(float(lw) / float(P.width), float(lh) / float(P.height)) - 0.5;
    float2 cellBase = floor(f);
    float2 frac = f - cellBase;
    int2 base = int2(cellBase);
    float4 sum = float4(0.0);
    float wsum = 0.0;
    for (int dy = 0; dy <= 1; dy++) {
        for (int dx = 0; dx <= 1; dx++) {
            int2 c = clamp(base + int2(dx, dy), int2(0), int2(int(lw) - 1, int(lh) - 1));
            float2 s = (float2(c) + 0.5) * float2(float(P.width) / float(lw), float(P.height) / float(lh));
            uint2 src = min(uint2(s), uint2(P.width - 1, P.height - 1));
            float d = positionIn.read(src).w;
            float bx = dx == 1 ? frac.x : 1.0 - frac.x;
            float by = dy == 1 ? frac.y : 1.0 - frac.y;
            float w = bx * by / (0.05 + abs(d - depth));
            sum += lo.read(uint2(c)) * w;
            wsum += w;
        }
    }
    return sum / max(wsum, 1e-5);
}

struct Upsampled {
    float4 value;
    bool valid;  // false: no low-resolution sample lies on this pixel's surface
};

// Edge-aware upsampling. Of the four nearest low-resolution samples, blend only
// those taken on the same surface as this pixel: similar depth (within 5%) and
// facing a similar way (normals within about 37 degrees). The normal test
// matters at an object's base, where the object's side and the ground just
// behind it are at almost the same depth. If no sample qualifies, the caller
// computes the value at full resolution for this pixel.
Upsampled upsampleEdgeAware(texture2d<float, access::read> lo,
                            texture2d<float, access::read> positionIn,
                            texture2d<float, access::read> normalIn,
                            uint2 gid, float depth, float3 n, constant Params &P) {
    Upsampled result;
    uint lw = lo.get_width();
    uint lh = lo.get_height();
    if (lw == P.width && lh == P.height) {
        result.value = lo.read(gid);
        result.valid = true;
        return result;
    }
    float2 f = (float2(gid) + 0.5) * float2(float(lw) / float(P.width), float(lh) / float(P.height)) - 0.5;
    float2 cellBase = floor(f);
    float2 frac = f - cellBase;
    int2 base = int2(cellBase);
    float tolerance = 0.05 * depth + 0.05;
    float4 sum = float4(0.0);
    float wsum = 0.0;
    for (int dy = 0; dy <= 1; dy++) {
        for (int dx = 0; dx <= 1; dx++) {
            int2 c = clamp(base + int2(dx, dy), int2(0), int2(int(lw) - 1, int(lh) - 1));
            float2 s = (float2(c) + 0.5) * float2(float(P.width) / float(lw), float(P.height) / float(lh));
            uint2 src = min(uint2(s), uint2(P.width - 1, P.height - 1));
            float d = positionIn.read(src).w;
            float3 sn = normalIn.read(src).xyz;
            bool sameSurface = d >= 0.0 && abs(d - depth) <= tolerance && dot(sn, n) > 0.8;
            if (sameSurface) {
                float bx = dx == 1 ? frac.x : 1.0 - frac.x;
                float by = dy == 1 ? frac.y : 1.0 - frac.y;
                float w = bx * by + 0.001;  // never exactly zero, so a lone match still counts
                sum += lo.read(uint2(c)) * w;
                wsum += w;
            }
        }
    }
    result.valid = wsum > 0.0;
    result.value = result.valid ? sum / wsum : float4(0.0);
    return result;
}

kernel void composite_kernel(texture2d<float, access::read> positionIn [[texture(0)]],
                             texture2d<float, access::read> normalIn [[texture(1)]],
                             texture2d<float, access::read> lightIn [[texture(2)]],
                             texture2d<float, access::read> aoIn [[texture(3)]],
                             texture2d<float, access::write> frameOut [[texture(4)]],
                             constant Params &P [[buffer(0)]],
                             uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= P.width || gid.y >= P.height) return;
    if (gid.x >= frameOut.get_width() || gid.y >= frameOut.get_height()) return;
    float3 ro;
    float3 rd;
    cameraRay(P.time, gid, P.width, P.height, ro, rd);
    float4 pos = positionIn.read(gid);
    float3 color;
    if (pos.w < 0.0) {
        color = skyColor(rd);
    } else {
        float3 n = normalIn.read(gid).xyz;
        float3 direct;
        float ao;
        if (P.upsampleMode == 1) {
            Upsampled light = upsampleEdgeAware(lightIn, positionIn, normalIn, gid, pos.w, n, P);
            direct = light.valid ? light.value.rgb : directLight(pos.xyz, n, P);
            Upsampled occ = upsampleEdgeAware(aoIn, positionIn, normalIn, gid, pos.w, n, P);
            ao = occ.valid ? occ.value.r : ambientOcclusion(pos.xyz, n, P);
        } else {
            direct = upsample(lightIn, positionIn, gid, pos.w, P).rgb;
            ao = upsample(aoIn, positionIn, gid, pos.w, P).r;
        }
        bool ground = pos.y < 0.1 && n.y > 0.9;
        float checker = ((int(floor(pos.x) + floor(pos.z)) & 1) == 0) ? 0.55 : 0.45;
        float3 albedo = ground ? float3(checker) : float3(0.75, 0.72, 0.68);
        float3 ambient = mix(float3(0.10, 0.09, 0.08), float3(0.30, 0.38, 0.50), float3(n.y * 0.5 + 0.5)) * ao;
        color = albedo * (direct + ambient);
        float fog = 1.0 - exp(-0.0012 * pos.w * pos.w);
        color = mix(color, skyColor(rd), float3(fog));
    }
    color = color * (2.51 * color + 0.03) / (color * (2.43 * color + 0.59) + 0.14);  // ACES fit
    color = pow(clamp(color, float3(0.0), float3(1.0)), float3(1.0 / 2.2));
    frameOut.write(float4(color, 1.0), gid);
}
"""

// MARK: - Entry point

let options = parseOptions()
// AppKit must be driven from the main thread; this states that explicitly so
// the file compiles the same way under Swift 5 and Swift 6 language rules.
MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let delegate = AppDelegate(options: options)
    app.delegate = delegate
    app.run()
}

import ARKit
import SceneKit
import SwiftUI
import VisionKit

struct ContentView: View {
    @AppStorage("relayURL") private var relayURL = "ws://192.168.1.10:8000"
    @AppStorage("responderID") private var responderID = "A"
    @State private var running = false
    @State private var status = "idle"
    @State private var streamer = Streamer()
    @State private var scanning = false
    @State private var talking = false

    var body: some View {
        ZStack(alignment: .bottom) {
            ARPreview(session: streamer.session).ignoresSafeArea()
            VStack(alignment: .leading, spacing: 10) {
                if !running {
                    HStack {
                        TextField("Relay URL", text: $relayURL)
                            .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                        Button("Scan") { scanning = true }      // QR from the hub's Connect button
                            .disabled(!DataScannerViewController.isSupported)
                    }
                    TextField("Responder ID", text: $responderID)
                        .textInputAutocapitalization(.characters).autocorrectionDisabled()
                    Text("Stand on the start mark facing the marked wall, then tap Start.")
                        .font(.footnote).foregroundStyle(.secondary)
                } else {
                    Text(status).font(.footnote.monospaced())
                    // party-line call: everyone else and the hub hear this phone while it is held
                    Text(talking ? "Talking…" : "Hold to talk")
                        .font(.title2.bold())
                        .frame(maxWidth: .infinity, minHeight: 90)
                        .background(talking ? Color.red : Color.accentColor.opacity(0.85), in: RoundedRectangle(cornerRadius: 14))
                        .foregroundStyle(.white)
                        .gesture(DragGesture(minimumDistance: 0)
                            .onChanged { _ in if !talking { talking = true; streamer.setTalking(true) } }
                            .onEnded { _ in talking = false; streamer.setTalking(false) })
                        .accessibilityAddTraits(.isButton)
                }
                Button(running ? "Stop" : "Start", action: toggle)
                    .buttonStyle(.borderedProminent)
                    .tint(running ? .red : .accentColor)
                    .frame(maxWidth: .infinity)
            }
            .textFieldStyle(.roundedBorder)
            .padding()
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
            .padding()
        }
        .sheet(isPresented: $scanning) {
            QRScanner { text in
                // hub encodes ws(s)://host[#ID]
                guard var c = URLComponents(string: text), c.scheme?.hasPrefix("ws") == true else { return }
                if let id = c.fragment, !id.isEmpty { responderID = id }
                c.fragment = nil
                relayURL = c.string ?? text
                scanning = false
            }
        }
    }

    private func toggle() {
        if running {
            if talking { talking = false; streamer.setTalking(false) }
            streamer.stop()
            running = false
            status = "idle"
            return
        }
        guard let url = URL(string: relayURL.trimmingCharacters(in: .whitespaces)), url.scheme?.hasPrefix("ws") == true else {
            status = "Relay URL must start with ws://"
            return
        }
        streamer.onStatus = { s in DispatchQueue.main.async { status = s } }
        streamer.start(url: url, id: responderID.trimmingCharacters(in: .whitespaces))
        running = true
    }
}

struct ARPreview: UIViewRepresentable {
    let session: ARSession
    func makeUIView(context: Context) -> ARSCNView {
        let v = ARSCNView()
        v.session = session
        v.automaticallyUpdatesLighting = false
        return v
    }
    func updateUIView(_ uiView: ARSCNView, context: Context) {}
}

/// Full-screen QR scanner (VisionKit). Calls `found` with the first QR payload seen.
struct QRScanner: UIViewControllerRepresentable {
    let found: (String) -> Void
    func makeUIViewController(context: Context) -> DataScannerViewController {
        let v = DataScannerViewController(recognizedDataTypes: [.barcode(symbologies: [.qr])], isHighlightingEnabled: true)
        v.delegate = context.coordinator
        try? v.startScanning()
        return v
    }
    func updateUIViewController(_ v: DataScannerViewController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(found: found) }
    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let found: (String) -> Void
        init(found: @escaping (String) -> Void) { self.found = found }
        func dataScanner(_ s: DataScannerViewController, didAdd items: [RecognizedItem], allItems: [RecognizedItem]) {
            for case .barcode(let b) in items { if let p = b.payloadStringValue { found(p); return } }
        }
    }
}

#Preview {
    ContentView()
}

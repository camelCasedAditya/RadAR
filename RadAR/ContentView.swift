import ARKit
import SceneKit
import SwiftUI

struct ContentView: View {
    @AppStorage("relayURL") private var relayURL = "ws://192.168.1.10:8000"
    @AppStorage("responderID") private var responderID = "A"
    @State private var running = false
    @State private var status = "idle"
    @State private var streamer = Streamer()

    var body: some View {
        ZStack(alignment: .bottom) {
            ARPreview(session: streamer.session).ignoresSafeArea()
            VStack(alignment: .leading, spacing: 10) {
                if !running {
                    TextField("Relay URL", text: $relayURL)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    TextField("Responder ID", text: $responderID)
                        .textInputAutocapitalization(.characters).autocorrectionDisabled()
                    Text("Stand on the start mark facing the marked wall, then tap Start.")
                        .font(.footnote).foregroundStyle(.secondary)
                } else {
                    Text(status).font(.footnote.monospaced())
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
    }

    private func toggle() {
        if running {
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

#Preview {
    ContentView()
}

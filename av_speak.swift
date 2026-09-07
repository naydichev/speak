// AVSpeechSynthesizer backend. Two things /usr/bin/say cannot do:
//
//   1. Personal Voice — gated behind a runtime authorization request.
//   2. SSML, so <prosody> emphasis that actually changes the waveform.
//      Measured on macOS 26: `say`'s [[emph +]] and SSML <emphasis> both
//      produce byte-identical audio, i.e. they are parsed and discarded.
//      <prosody pitch/rate/volume> is the only thing that really works.
//
//   av_speak --list                        list voices, personal ones marked
//   av_speak <voice> <wpm> <text|ssml>     speak ("" = default, 0 = default rate)

import AVFoundation
import Foundation

// Calibration knob: the words-per-minute that utterance rate 0.5 sounds like.
// `say -r` is absolute wpm, AVSpeechUtterance.rate is 0...1 around a 0.5
// default, so the two backends only agree on a number if this matches the
// voice. Nudge it if /rate 200 sounds different across backends.
let defaultWPM = 175.0

func isPersonal(_ v: AVSpeechSynthesisVoice) -> Bool {
    v.voiceTraits.contains(.isPersonalVoice)
}

/// Personal voices are absent from speechVoices() until the user grants access.
/// Prompts at most once ever; a previous denial returns immediately.
@discardableResult
func authorize() -> AVSpeechSynthesizer.PersonalVoiceAuthorizationStatus {
    let now = AVSpeechSynthesizer.personalVoiceAuthorizationStatus
    if now != .notDetermined { return now }

    var status = now
    let sem = DispatchSemaphore(value: 0)
    AVSpeechSynthesizer.requestPersonalVoiceAuthorization {
        status = $0
        sem.signal()
    }
    sem.wait()

    return status
}

func find(_ want: String) -> AVSpeechSynthesisVoice? {
    func look() -> AVSpeechSynthesisVoice? {
        AVSpeechSynthesisVoice(identifier: want)
            ?? AVSpeechSynthesisVoice.speechVoices().first { $0.name == want }
    }

    if let v = look() { return v }

    authorize()             // the name may belong to a personal voice
    return look()
}

final class Done: NSObject, AVSpeechSynthesizerDelegate {
    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        CFRunLoopStop(CFRunLoopGetMain())
    }
    func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel u: AVSpeechUtterance) {
        CFRunLoopStop(CFRunLoopGetMain())
    }
}

func die(_ msg: String, _ code: Int32) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(code)
}

let args = Array(CommandLine.arguments.dropFirst())

if args.first == "--list" {
    // Listing is also where the one-time Personal Voice prompt happens, so
    // picking a voice reveals them instead of hiding them behind a flag.
    let status = authorize()

    for v in AVSpeechSynthesisVoice.speechVoices() {
        print("\(v.name)\t\(v.identifier)\(isPersonal(v) ? "\tpersonal" : "")")
    }

    if status != .authorized {
        FileHandle.standardError.write("""
            note: Personal Voice is \(status == .denied ? "denied" : "unavailable") — \
            create one in Settings > Accessibility > Personal Voice and turn on \
            "Allow Apps to Request to Use".\n
            """.data(using: .utf8)!)
    }
    exit(0)
}

guard args.count >= 3 else {
    die("usage: av_speak <voice> <wpm> <text|ssml>   |   av_speak --list", 2)
}

let text = args[2...].joined(separator: " ")

// speak.py sends SSML only when the line actually has emphasis in it
let utterance: AVSpeechUtterance
if text.hasPrefix("<speak") {
    guard let u = AVSpeechUtterance(ssmlRepresentation: text) else {
        die("av_speak: could not parse SSML", 3)
    }
    utterance = u
} else {
    utterance = AVSpeechUtterance(string: text)
}

if !args[0].isEmpty {
    guard let v = find(args[0]) else { die("av_speak: no voice named \(args[0])", 4) }
    utterance.voice = v
}

if let wpm = Double(args[1]), wpm > 0 {
    utterance.rate = min(
        max(Float(0.5 * wpm / defaultWPM), AVSpeechUtteranceMinimumSpeechRate),
        AVSpeechUtteranceMaximumSpeechRate)
}

let synth = AVSpeechSynthesizer()
let done = Done()
synth.delegate = done

synth.speak(utterance)
CFRunLoopRun()

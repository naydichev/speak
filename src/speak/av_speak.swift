// AVSpeechSynthesizer backend. It exists for SSML, which /usr/bin/say cannot
// speak, and SSML is the only route to pitch — measured on macOS 26, `say`'s
// [[emph +]], [[pbas]] and [[volm]] and SSML's own <emphasis> all produce
// byte-identical audio, i.e. they are parsed and discarded. Only
// <prosody pitch/rate> in percent form actually moves the waveform.
//
// It also requests Personal Voice authorization. That is NOT exclusive to this
// backend: once a personal voice is trained and granted, `say -v ?` lists it
// and `say -v <name>` really speaks it (verified against the fallback).
//
//   av_speak --list                    list voices, personal ones marked
//   av_speak <voice> <text|ssml>       speak ("" = the default voice)
//
// There is no rate argument. AVSpeechUtterance.rate is ignored outright on an
// SSML utterance, and is non-linear besides — 225wpm mapped onto it played
// 1.82x faster than default where `say -r 225` is 1.28x. speak.py puts speed
// in the SSML as a percentage, which IS linear in wpm.

import AVFoundation
import Foundation

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

    // Bounded: the prompt is a system dialog, and an unanswered one would
    // otherwise hang --list forever, freezing the caller's whole UI.
    _ = sem.wait(timeout: .now() + 20)

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
        print("\(v.name)\t\(v.identifier)\t\(v.language)\(isPersonal(v) ? "\tpersonal" : "")")
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

guard args.count >= 2 else {
    die("usage: av_speak <voice> <text|ssml>   |   av_speak --list", 2)
}

let text = args[1...].joined(separator: " ")

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

let synth = AVSpeechSynthesizer()
let done = Done()
synth.delegate = done

synth.speak(utterance)
CFRunLoopRun()

import { useEffect, useState } from "react";
import { StatusBar } from "expo-status-bar";
import * as Speech from "expo-speech";
import AsyncStorage from "@react-native-async-storage/async-storage";
import {
  Linking,
  Pressable,
  SafeAreaView,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { GEMINI_KEY, GEMINI_MODELS } from "./config";

const QUOTE = "You do not have to speak loudly to be heard.\nA look, a breath, or a single word is enough.";

const PHRASES = [
  "Hello",
  "Thank you",
  "Yes",
  "No",
  "I need help",
  "Water please",
  "I'm in pain",
  "I love you",
];

const QUESTIONS = [
  ["Explain my condition simply", "Explain my condition simply, without pretending you know my chart."],
  ["Ease muscle stiffness", "How can a caregiver safely help muscle stiffness?"],
  ["Calm breathing", "Lead a short calm breathing exercise in a few lines."],
  ["Better sleep", "Give a few simple sleep tips for someone who is unwell."],
  ["I feel pain", "I feel pain now. What can a caregiver safely do?"],
];

const VIDEOS = [
  ["Religion & Spiritual", "Ethiopian Mezmur"],
  ["Music", "Oromo Traditional Music"],
  ["Comedy", "Funny Comedy Clips"],
  ["Learning", "Stephen Hawking Space Documentary"],
];

const STORY =
  "Once there was a small lamp in a quiet room. The night felt long, but the lamp stayed lit. A friend sat beside it and said, you do not have to shine for the whole world. Shine for this moment.";

async function askGemini(prompt) {
  const body = JSON.stringify({
    contents: [{ parts: [{ text: prompt }] }],
    generationConfig: { maxOutputTokens: 400, temperature: 0.4 },
  });
  let last = "The assistant is busy. Here is a quiet guide instead.";
  for (const model of GEMINI_MODELS) {
    try {
      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-goog-api-key": GEMINI_KEY },
          body,
        }
      );
      if (!response.ok) {
        last = response.status === 503 ? last : "The assistant could not answer just now.";
        continue;
      }
      const data = await response.json();
      const text = data?.candidates?.[0]?.content?.parts?.map((part) => part.text || "").join(" ");
      if (text) return text.trim();
    } catch {
      last = "The assistant needs an internet connection.";
    }
  }
  return last;
}

function Tile({ label, color, onPress }) {
  return (
    <Pressable onPress={onPress} style={[styles.tile, { backgroundColor: color }]}>
      <Text style={styles.tileText}>{label}</Text>
    </Pressable>
  );
}

export default function App() {
  const [screen, setScreen] = useState("welcome");
  const [name, setName] = useState("");
  const [origin, setOrigin] = useState("");
  const [condition, setCondition] = useState("");
  const [line, setLine] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    AsyncStorage.getItem("ican-profile").then((saved) => {
      if (!saved) return;
      const profile = JSON.parse(saved);
      setName(profile.name || "");
      setOrigin(profile.origin || "");
      setCondition(profile.condition || "");
    }).catch(() => {});
  }, []);

  function speak(text, times = 1) {
    const spoken = (text || "").trim();
    if (!spoken) {
      setNote("Nothing to read yet.");
      return;
    }
    Speech.stop();
    let left = times;
    const once = () => {
      Speech.speak(spoken, {
        onDone: () => {
          left -= 1;
          if (left > 0) once();
        },
      });
    };
    once();
    setNote("Speaking once. Stop can cut it off.");
  }

  function stop() {
    Speech.stop();
    setNote("Quiet.");
  }

  async function saveProfile() {
    const profile = { name: name.trim(), origin: origin.trim(), condition: condition.trim() };
    await AsyncStorage.setItem("ican-profile", JSON.stringify(profile));
    setScreen("hub");
  }

  async function ask(prompt) {
    setNote("Asking. It will not speak until you press Hear.");
    const memory = [
      name ? `Their name is ${name}.` : "",
      origin ? `They are from ${origin}.` : "",
      condition ? `They are living with ${condition}. Do not invent symptoms or doses.` : "",
    ].filter(Boolean).join(" ");
    const answer = await askGemini(`${memory}\nAnswer in simple warm words.\n${prompt}`);
    setLine(answer);
    setNote("Ready. Press Hear when you want this read.");
  }

  function openVideo(query) {
    const url = `https://www.youtube.com/results?search_query=${encodeURIComponent(query)}&sp=CAM%3D`;
    Linking.openURL(url);
    setNote("YouTube is opening. Come back with First page.");
  }

  return (
    <SafeAreaView style={styles.safe}>
      <StatusBar style="light" />
      {screen === "welcome" && (
        <View style={[styles.page, { backgroundColor: "#2C1454" }]}>
          <View style={styles.card}>
            <Text style={styles.brand}>I Can</Text>
            <Text style={styles.hope}>Give hope a voice.</Text>
            <Text style={styles.quote}>{QUOTE}</Text>
            <Text style={styles.made}>This place was made for you.</Text>
            <Tile label="Begin" color="#F2A63A" onPress={() => setScreen("register")} />
          </View>
        </View>
      )}

      {screen === "register" && (
        <ScrollView contentContainerStyle={[styles.page, { backgroundColor: "#123C46" }]}>
          <View style={styles.card}>
            <Text style={styles.brand}>A small hello</Text>
            <Text style={styles.quote}>A caregiver can fill this in. You can skip it.</Text>
            <Text style={styles.label}>Your name</Text>
            <TextInput value={name} onChangeText={setName} style={styles.input} />
            <Text style={styles.label}>Where you are from</Text>
            <TextInput value={origin} onChangeText={setOrigin} style={styles.input} />
            <Text style={styles.label}>What you are living with</Text>
            <TextInput value={condition} onChangeText={setCondition} style={styles.input} />
            <Tile label="Continue" color="#1F8A70" onPress={saveProfile} />
            <Tile label="Skip" color="#F2C14E" onPress={() => setScreen("hub")} />
          </View>
        </ScrollView>
      )}

      {screen === "hub" && (
        <ScrollView contentContainerStyle={[styles.page, { backgroundColor: "#3A1860" }]}>
          <Text style={styles.hope}>Where shall we go?</Text>
          <Text style={styles.made}>Touch chooses. Hear speaks. Stop keeps it quiet.</Text>
          <View style={styles.grid}>
            <Tile label="Talk" color="#7DDFC3" onPress={() => setScreen("talk")} />
            <Tile label="AI Assistant" color="#8EB6FF" onPress={() => setScreen("ai")} />
            <Tile label="A story" color="#C9A6FF" onPress={() => { setScreen("story"); setLine(STORY); }} />
            <Tile label="YouTube" color="#F2A3C7" onPress={() => setScreen("video")} />
            <Tile label="I need help" color="#F08A7E" onPress={() => speak("Emergency! I need immediate assistance.", 3)} />
            <Tile label="First page" color="#F2C14E" onPress={() => { stop(); setScreen("welcome"); }} />
          </View>
          <Text style={styles.note}>{note}</Text>
        </ScrollView>
      )}

      {screen !== "welcome" && screen !== "register" && screen !== "hub" && (
        <View style={styles.bar}>
          <Pressable onPress={() => { stop(); setScreen("hub"); }}><Text style={styles.barText}>Home</Text></Pressable>
          <Pressable onPress={() => speak(line || "Hello")}><Text style={styles.barText}>Hear</Text></Pressable>
          <Pressable onPress={stop}><Text style={styles.barText}>Stop</Text></Pressable>
          <Pressable onPress={() => { stop(); setScreen("welcome"); }}><Text style={styles.barText}>First page</Text></Pressable>
        </View>
      )}

      {screen === "talk" && (
        <ScrollView contentContainerStyle={styles.work}>
          <Text style={styles.line}>{line || "Choose a phrase. It stays quiet until Hear."}</Text>
          {PHRASES.map((phrase) => (
            <Tile key={phrase} label={phrase} color="#FFF6E4" onPress={() => { setLine(phrase); setNote("Press Hear."); }} />
          ))}
        </ScrollView>
      )}

      {screen === "ai" && (
        <ScrollView contentContainerStyle={styles.work}>
          <Text style={styles.line}>{line || "Ask something. The answer waits for Hear."}</Text>
          <TextInput
            value={line}
            onChangeText={setLine}
            placeholder="Or type a question"
            placeholderTextColor="#8A7494"
            style={styles.input}
          />
          {QUESTIONS.map(([label, prompt]) => (
            <Tile key={label} label={label} color="#E7EEFF" onPress={() => ask(prompt)} />
          ))}
          <Tile label="Ask what I typed" color="#8EB6FF" onPress={() => ask(line)} />
          <Text style={styles.note}>{note}</Text>
        </ScrollView>
      )}

      {screen === "story" && (
        <ScrollView contentContainerStyle={styles.work}>
          <Text style={styles.line}>{line}</Text>
          <Tile label="A new story" color="#C9A6FF" onPress={() => ask("Tell a gentle hopeful story of about 120 words. No medicine.")} />
          <Text style={styles.note}>{note}</Text>
        </ScrollView>
      )}

      {screen === "video" && (
        <ScrollView contentContainerStyle={styles.work}>
          <Text style={styles.line}>Opens the most-watched results in YouTube.</Text>
          {VIDEOS.map(([label, query]) => (
            <Tile key={query} label={label} color="#FDE4EF" onPress={() => openVideo(query)} />
          ))}
          <Text style={styles.note}>{note}</Text>
        </ScrollView>
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: "#2C1454" },
  page: { flexGrow: 1, alignItems: "center", justifyContent: "center", padding: 20 },
  card: { backgroundColor: "#FFF6E4", borderRadius: 28, padding: 24, width: "100%", maxWidth: 460 },
  brand: { fontSize: 54, fontWeight: "800", color: "#E25B45", textAlign: "center" },
  hope: { fontSize: 26, fontWeight: "800", color: "#FFF6E4", textAlign: "center", marginBottom: 8 },
  quote: { fontSize: 18, color: "#5C3D73", textAlign: "center", marginVertical: 10 },
  made: { fontSize: 16, fontWeight: "700", color: "#C46B2C", textAlign: "center", marginBottom: 16 },
  label: { color: "#1F8A70", fontWeight: "700", marginTop: 10 },
  input: { backgroundColor: "#FFFFFF", borderRadius: 14, borderWidth: 2, borderColor: "#F2A63A", padding: 12, fontSize: 16, marginBottom: 8 },
  grid: { width: "100%", maxWidth: 460 },
  tile: { borderRadius: 22, paddingVertical: 18, paddingHorizontal: 16, marginVertical: 6, alignItems: "center" },
  tileText: { fontSize: 20, fontWeight: "800", color: "#241428" },
  bar: { flexDirection: "row", justifyContent: "space-around", paddingVertical: 12, backgroundColor: "#1B1030" },
  barText: { color: "#FFF6E4", fontWeight: "800", fontSize: 16 },
  work: { padding: 16, backgroundColor: "#F7EFE3" },
  line: { fontSize: 20, color: "#241428", marginBottom: 12 },
  note: { color: "#5C3D73", textAlign: "center", margin: 12 },
});

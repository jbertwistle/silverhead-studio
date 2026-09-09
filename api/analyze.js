// Vercel Serverless Function: /api/analyze
// Keep OPENAI_API_KEY in Vercel Environment Variables. Never put it in index.html.

export default async function handler(req,res){
  res.setHeader("Content-Type","application/json");
  if(req.method!=="POST") return res.status(405).json({error:"POST only"});
  if(!process.env.OPENAI_API_KEY) return res.status(500).json({error:"OPENAI_API_KEY is not configured in Vercel."});

  try{
    const {session,recordingUrl,frames=[]}=req.body||{};
    if(!recordingUrl) return res.status(400).json({error:"Missing recording URL."});
    if(!Array.isArray(frames)||frames.length===0) return res.status(400).json({error:"No video frames were supplied."});

    // 1) AUDIO / TEXT CHANNEL
    const mediaResponse=await fetch(recordingUrl);
    if(!mediaResponse.ok) throw new Error(`Could not fetch raw recording (${mediaResponse.status}).`);
    const mediaBlob=await mediaResponse.blob();
    const contentType=mediaResponse.headers.get("content-type")||mediaBlob.type||"video/webm";
    const extension=contentType.includes("mp4")?"mp4":"webm";

    const form=new FormData();
    form.append("model","gpt-4o-transcribe");
    form.append("file",mediaBlob,`session.${extension}`);

    const transcribeResponse=await fetch("https://api.openai.com/v1/audio/transcriptions",{
      method:"POST",
      headers:{Authorization:`Bearer ${process.env.OPENAI_API_KEY}`},
      body:form
    });
    const transcription=await transcribeResponse.json();
    if(!transcribeResponse.ok) throw new Error(transcription?.error?.message||"Audio transcription failed.");
    const transcript=transcription.text||"";

    // 2) VISUAL CHANNEL + 3) SYNTHESIS
    const frameLegend=frames.map((f,i)=>`Image ${i+1}: approximately ${Number(f.time).toFixed(1)} seconds.`).join("\n");
    const prompt=`You are analyzing embodied improvisation research footage.

Treat the raw recording as evidence. Do not infer emotions, diagnoses, intentions, identities, or off-screen events from appearance. Do not invent visual events from the transcript.

SESSION
Performers label: ${session?.performers||"unspecified"}
Duration: ${session?.duration_seconds||"unknown"} seconds
Performer notes: ${session?.notes||"none"}

TRANSCRIPT
${transcript||"[no intelligible speech transcribed]"}

VIDEO SAMPLE TIMESTAMPS
${frameLegend}

Return ONLY valid JSON with this exact shape:
{
  "text_patterns": ["3-8 concise observations about repeated words, phrases, images, questions, verbal structures, or conspicuous absences. Do not claim precise timestamps because the transcript is not yet timecoded."],
  "visual_observations": [
    {"time": 0.0, "observation": "Concrete visible description based only on the corresponding sampled frame."}
  ],
  "possible_connections": ["0-5 explicitly tentative connections between audio/text and visible material. Use words such as possible, may, or appears. Never state interpretation as fact."]
}

For visual observations, use only the supplied frame timestamps. Describe concrete body position, orientation, spacing, entrance/exit from frame, repeated posture, visible gesture, or stillness when genuinely visible. If a frame does not support a useful observation, omit it.`;

    const content=[{type:"input_text",text:prompt}];
    for(const f of frames) content.push({type:"input_image",image_url:f.image});

    const visionResponse=await fetch("https://api.openai.com/v1/responses",{
      method:"POST",
      headers:{
        Authorization:`Bearer ${process.env.OPENAI_API_KEY}`,
        "Content-Type":"application/json"
      },
      body:JSON.stringify({
        model:"gpt-5.6-luna",
        input:[{role:"user",content}]
      })
    });
    const vision=await visionResponse.json();
    if(!visionResponse.ok) throw new Error(vision?.error?.message||"Visual analysis failed.");

    const outputText=(vision.output||[])
      .flatMap(item=>item.content||[])
      .filter(c=>c.type==="output_text"&&typeof c.text==="string")
      .map(c=>c.text)
      .join("\n")
      .trim();

    let parsed;
    try{
      const cleaned=outputText.replace(/^```(?:json)?\s*/i,"").replace(/\s*```$/,"");
      parsed=JSON.parse(cleaned);
    }catch{
      parsed={
        text_patterns:[],
        visual_observations:[],
        possible_connections:["The model returned an analysis that could not be parsed into the expected structure."],
        raw_model_output:outputText
      };
    }

    return res.status(200).json({
      transcript,
      text_patterns:Array.isArray(parsed.text_patterns)?parsed.text_patterns:[],
      visual_observations:Array.isArray(parsed.visual_observations)?parsed.visual_observations:[],
      possible_connections:Array.isArray(parsed.possible_connections)?parsed.possible_connections:[],
      raw_model_output:parsed.raw_model_output||undefined
    });
  }catch(error){
    console.error(error);
    return res.status(500).json({error:error?.message||"Analysis failed."});
  }
}

Use a free Hugging Face Docker Space for exact `rembg` quality.

1. Create a new public Hugging Face Space.
2. Choose `Docker` as the SDK.
3. Upload the contents of [hf-bg-removal-service](/c:/Users/Aaftaab%20Ahmad%20Khan/Documents/PW%20AI%20Hackathon/Digital%20Graphic%20Design%20Generator/hf-bg-removal-service).
4. Wait for the Space to build.
5. Set `BG_REMOVE_API_URL` in Vercel to your Space URL, for example:
   `https://your-space-name.hf.space`
6. Redeploy the Vercel app.

The main app already supports this env var and will call `/api/remove-bg` on that external service.

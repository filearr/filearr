<script lang="ts">
  // Production stand-in for #/kit: the gallery is a dev tool, but the route stays
  // so an old link lands here instead of falling through to Search.
  import StateBlock from "./ui/StateBlock.svelte";
</script>

<StateBlock state="empty" message="The UI kit gallery is only available in development builds." />
